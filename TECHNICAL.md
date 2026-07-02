# Flight — Guia Técnico de Implementação
### Como construir, fase por fase, mecanismo por mecanismo (v1.0)

Este documento é o par técnico do [VISION.md](VISION.md). Lá está o *o quê* e o *porquê*; aqui está o
**como**: os mecanismos exatos do CPython e do Rust, a ideia central de cada fase, e as armadilhas
conhecidas. As seções marcadas **[Fase 0 ✅]** já estão implementadas neste repositório.

---

## CAPÍTULO 0 — Os três mecanismos que sustentam tudo

### 0.1 — `sys.monitoring` (PEP 669): como o Flight "enxerga" a execução  **[Fase 0 ✅]**

O CPython 3.12+ expõe um sistema de eventos do interpretador:

1. Você reserva um **tool id** (6 slots, 0–5; debuggers usam o 0, coverage o 1 — pegamos o 2).
2. Registra **callbacks** para tipos de evento.
3. Ativa os eventos globalmente ou por objeto de código.

Eventos que o Flight usa por fase:

| Evento | Dispara quando | Fase |
|---|---|---|
| `PY_START` / `PY_RETURN` | função Python entra/sai | 0 |
| `RAISE` / `RERAISE` / `PY_UNWIND` | exceção levantada / re-levantada / desfaz frame | 0 |
| `LINE` | nova linha executa | 0 (opt-in) / 1 |
| `CALL` | chamada a qualquer callable (inclusive C) | 2/3 (fronteira C) |
| `INSTRUCTION` | cada bytecode | 2 (opção B de mutações — caro!) |

**As duas propriedades que tornam o projeto viável:**
- **`DISABLE` por localização:** retornar `sys.monitoring.DISABLE` de um callback desliga aquele evento
  *naquele ponto específico do código*. É como o `flight` desliga a stdlib/site-packages: paga o custo
  uma vez por localização e depois nada.
- **`set_local_events`:** ligar eventos só para funções específicas — o coração da Fase 2.

**Armadilhas conhecidas:**
- Callbacks NÃO podem levantar exceção (derrubaria o programa — viola P1). Todo callback é `try/except`
  total; no Rust, `catch_unwind`.
- O callback roda com o GIL, no meio da execução do usuário. Nada de I/O; só empurrar bytes num ring.
- `LINE` em código massivamente hot custa mesmo com callback barato. Mitigação: `DISABLE` agressivo +
  denylist de módulos (stdlib e site-packages desligados por padrão). No Flight, `record_lines` é
  **opt-in** justamente por isso.

### 0.2 — PyO3 + maturin: como o Rust vira um módulo Python  **[Fase 0 ✅]**

`maturin develop` compila e instala no venv; `maturin build --release` gera a wheel. Detalhe de
performance: a fronteira Python→Rust custa ~50–100 ns por chamada, e o *dispatch* do callback Python do
`sys.monitoring` custa mais algumas centenas de ns.

> **Otimização pendente (a principal da Fase 1):** hoje o callback é uma função Python que chama o
> Rust — o baseline "didático". O caminho para o alvo de <5% (P2) em código hot é registrar o callback
> como **função nativa** (`PyCFunction` gerado pelo PyO3), eliminando o trampolim Python inteiro. O
> baseline medido em `scripts/bench.py` (~350–500 ns/evento) é dominado exatamente por esse trampolim,
> não pelo ring buffer em Rust. Ver a seção "Overhead" do [README](README.md).

Todo entry point Rust exposto ao interpretador usa `std::panic::catch_unwind` e **nunca** propaga um
pânico para o CPython (P1).

### 0.3 — O pipeline de escrita: ring na frente, disco atrás  **[Fase 0 ✅]**

Regra: **o caminho quente nunca toca disco.**

```
callback (hot, ~ns)              no crash / capture()
     │                                  │
     ▼                                  ▼
[ring buffer lock-free] ──drain──► [merge por tstamp] ──► [msgpack] ──► [zstd] ──► crash.flight
```

Na Fase 0 o ring só é drenado no crash ou num `capture()` explícito. Na Fase 2, uma thread de fundo o
drena continuamente. O `Event` é uma struct fixa de 24 bytes; um ring por thread (`thread_local` na
otimização final; hoje um `HashMap<thread, Ring>` protegido por mutex, uncontended sob o GIL).

---

## CAPÍTULO 1 — FASE 1: a caixa-preta completa

> **Estado:** a Fase 0 já entrega o *rear-view mirror* (o ring de eventos). A Fase 1 adiciona, no
> momento do crash, os **frames + locals + grafo de objetos + código-fonte**.

### 1.1 A ideia central

Durante a execução normal, o Flight só empurra eventos num ring (barato). Quando uma exceção **não
tratada** acontece, aí sim fazemos o trabalho caro, **uma única vez, num processo já condenado**:
congelamos os frames, serializamos cada variável local como um grafo de objetos, anexamos o
código-fonte e o conteúdo do ring, e gravamos tudo num `.flight`. A assimetria é a sacada: overhead
~zero em vida normal, esforço total no momento da morte.

### 1.2 Interceptar "a morte" (todos os caminhos)  **[parcial na Fase 0]**

Um processo Python morre por exceção por 4 portas; a Fase 0 já cobre `sys.excepthook` e
`threading.excepthook` (gerando o `.flight`). A Fase 1 acrescenta `sys.unraisablehook` e o handler de
`asyncio`, além da captura manual `flight.capture(e)` para exceções *tratadas* mas suspeitas (já
disponível como `flight.capture()`).

### 1.3 O algoritmo de captura do crash

Percorrer `tb.tb_next` até o fim; para cada frame (do mais próximo do crash para o mais distante —
prioridade se o orçamento estourar), tirar `dict(frame.f_locals)` e serializar o grafo respeitando um
`deadline` (default 250 ms) e `max_bytes` (default 20 MB). Anexar fontes (`linecache` + hash), drenar o
ring, seguir a cadeia `__cause__`/`__context__`. Tudo dentro de um `try` total: qualquer falha grava o
que tem e marca o arquivo como `partial`.

### 1.4 O serializador de grafo (o algoritmo mais importante da fase)

Serializar objetos arbitrários sem executar código perigoso, sem loop infinito em ciclos, preservando
**identidade** (aliasing), com orçamento. `seen[id(obj)]` detecta ciclos e aliasing; tipos nativos por
valor (str/bytes truncados guardando `len` real + hash); contêineres com limites (K itens,
profundidade D); objetos genéricos via `vars()`/`__slots__` + `safe_repr`; adaptadores plugáveis para
ndarray/DataFrame. `safe_repr` captura `BaseException` — a defesa real contra um `__repr__` lento é o
**deadline global**. Scrubbing (P5) acontece dentro do `describe` para chaves/atributos sensíveis.

### 1.5 O ring buffer em Rust  **[Fase 0 ✅]**

Array circular de `Event` (24 bytes), tamanho potência de 2 (índice por máscara, não `%`). `head` é um
`AtomicUsize`; o push é um `fetch_add` + um store. `tstamp` é um contador atômico global (timestamp
*lógico*) que ordena eventos entre threads sem custo de relógio. **Não** guardamos o objeto código
(referência cíclica) — guardamos `id(code)` e um mapa lateral `code_id → (arquivo, qualname,
first_line)` alimentado no primeiro `PY_START`. Ver `crates/flight-core/src/ring.rs` e `recorder.rs`.

### 1.6 Estrutura de arquivos (estado atual)

```
crates/
  flight-format/   src/{lib,block,event,header,writer,error}.rs
  flight-reader/   src/lib.rs + tests/roundtrip.rs
  flight-core/     src/{lib,ring,recorder,dump}.rs   (módulo Python flight._core)
python/flight/     __init__, _install, _config, _read, _cli, __main__
tests/             test_recording.py, test_read_and_cli.py
scripts/bench.py   baseline de overhead
```

---

## CAPÍTULO 2 — FASE 1.5: o viewer (mecânica)  **[implementada]**

Uma aplicação [Textual](https://textual.textualize.io) que NUNCA entende bytes: consome a API do reader
via `flight.read(path)` → `Crash`/`Recording` (`frames`, `objects`, `sources`, `exceptions`,
`mutations`, `events`). O recurso-assinatura é o índice de aliases — "este MESMO dict aparece no frame 3
e no frame 9" (`_viewer_model.alias_index`, marcado com `↔` na árvore). Valores inline no código:
regex de identificadores nas linhas visíveis, anotando os `Name`s presentes nos locais do frame
(`inline_values`). Árvore com **expansão lazy** do grafo de objetos (populada em `on_tree_node_expanded`)
abre `.flight` grande instantaneamente.

**Implementação (o que existe):** `_viewer_model.py` — toda a lógica sem render (janela de código,
valores inline, índice de aliases, rótulos/filhos de nós, detalhe de objeto), **testável sem terminal**.
`_viewer.py` — o `App` Textual (casca fina): `Tree` à esquerda; abas Source/Detail/Exception/Events/
Timeline à direita; bindings `q` (sair), `a` (aliases do objeto sob o cursor), `e` (expandir frame).
CLI `flight view`; Textual é dependência **opcional** (`[viewer]`), com degradação clara se ausente. O
app é dirigido *headless* nos testes pelo `Pilot` do Textual (`app.run_test()`).

## CAPÍTULO 3 — FASE 2: time-travel de escopo  **[implementada]**

Dentro de `with flight.record():`, grava-se **toda escrita de estado** como eventos MUTATION. Estado(t)
= replay das mutações até t (event sourcing aplicado à memória). O guia pesa três opções: **(A)**
reescrita de bytecode (exata, porém frágil por versão do CPython), **(B)** evento `INSTRUCTION` (robusto
mas não lê o valor recém-empurrado na pilha), **(C)** proxies de contêiner (quebram `type(x) is dict`).

**O que o Flight implementa (a escolha robusta e à prova de versão):** por evento **LINE** dentro do
escopo (habilitado só enquanto há escopo ativo, via `set_events`), o `_record` faz **(1) diff dos
locais** do frame contra a linha anterior → rebinds de variáveis, e **(2) diff de snapshot** de cada
objeto sob `watch(obj)` → escritas em contêiner/atributo, *sem substituir o objeto* (não quebra
`type()` — é a ideia da opção C, mas não-invasiva). Como o evento LINE dispara *antes* da linha rodar, a
mudança detectada é atribuída à linha anterior executada — a que fez a escrita — dando **atribuição
exata**; e a última escrita de um frame (sem LINE seguinte) é recuperada no **PY_RETURN/PY_UNWIND**,
então nada é perdido. Sem cirurgia de bytecode, funciona em 3.12+. Cada MUTATION guarda um render raso do
valor (`kind/repr/type/length`) — o log é uma sequência de snapshots, exatamente o que um histórico por
variável precisa, mantendo o log pequeno. Um cap (`capture_max_mutations`) impede crescimento sem limite.

Consultas sobre o log (reader Python `Recording`): `history(nome)`, `who_mutated(nome)`, `state_at(seq)`.
CLI: `flight timeline [--var|--who]`.

**Passo futuro (opção A):** granularidade por instrução via instrumentação nativa de bytecode
(injetar `flight_core.mut(...)` após cada `STORE_*`, com o valor duplicado da pilha) — captura exata,
custo só onde instrumentado, ao preço de trabalho fino por versão do CPython. **Checkpoints (TIMELINE)**
para navegação O(1) e a **fronteira C** (checksum + evento `CALL`) também ficam para depois: o log de
mutações com valores completos já reconstrói qualquer instante por varredura (n é limitado ao escopo).

## CAPÍTULO 4 — FASE 3: replay determinístico

Um programa é função determinística dos seus inputs não-determinísticos (I/O, relógio, aleatoriedade,
env, escalonamento). Gravando **apenas** essas fontes nas bordas (barato), reexecuta-se o código
substituindo cada fonte pelo valor gravado — o bug se repete bit a bit. Degrau 1 (cedo): repro rasa —
gerar `repro_bug.py` reconstruindo os argumentos do frame do crash a partir do grafo de objetos.

## CAPÍTULO 5 — Ordem de ataque

```
Fase 0  ✅  repo, maturin, ring contando eventos, round-trip do formato, benchmark baseline.
Fase 1  ✅  excepthooks + captura de frames/locals + serializador de grafo + fontes + scrubbing.
Fase 2  ✅  with flight.record(): MUTATION via LINE-diff + watch(); timeline (history/who/state_at).
Fase 1.5 ✅ Viewer Textual (frames→locais→grafo→código inline→ring→timeline) sobre o reader.
Fase 3      repro rasa → interposição de fontes → threads.
```
