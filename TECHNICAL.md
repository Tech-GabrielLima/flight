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

`maturin develop --release` compila (em release) e instala no venv; `maturin build --release` gera a
wheel. **Detalhe crítico:** `maturin develop` sem `--release` compila em **debug** — ~10× mais lento no
caminho quente. Todos os números de overhead pressupõem release.

> **Otimização de hot path (feita).** Os callbacks do `sys.monitoring` são **funções Rust nativas**
> (`#[pyfunction]`) registradas direto — o interpretador chama o Rust sem frame de callback Python e sem
> segundo salto de FFI. Medição decisiva (release): o `sys.monitoring` despachando para um callback que
> **não faz nada** custa ~37 ns; com o push no ring lock-free ~55 ns; com filtro completo ~85 ns/evento.
> A descoberta contraintuitiva: um callback **Python** é despachado a ~45 ns, mas uma função PyO3 nativa
> em **debug** custava ~660 ns — a diferença era inteiramente o modo debug do Rust, não o PyO3. Em
> release a nativa vence a Python e chega perto do piso. Abaixo de ~37 ns é impossível com PEP 669 (é o
> custo do próprio `sys.monitoring`); só a instrumentação de bytecode (opção A da Fase 2) eliminaria o
> callback por evento, ao preço de fragilidade por versão do CPython. Ver "Overhead" no
> [README](README.md).

O ring é **lock-free por thread**: um cache thread-local guarda o ponteiro do ring da thread (validado
por uma *generation* que o `reset()` incrementa), então o push é um `fetch_add` atômico + um store de 24
bytes, sem mutex. A decisão interessante/deny por code id também é cacheada **sem lock** num cache
**direto thread-local** (indexado pelo ponteiro do código; validado por generation) — num loop quente o
acerto é ~100% e o caminho quente nunca toca o mutex; o mapa global (com hasher de inteiros rápido, não
SipHash) é a fonte da verdade no miss. Todo entry point Rust usa `catch_unwind` e **nunca** propaga um
pânico para o CPython (P1).

**Número final (release):** ~65 ns/evento gravado (dispatch ~40 + push lock-free + filtro cacheado
~25), ~2.5× em código patológico que chama uma função gravada por iteração, ~1.0× no caso comum.
**Por que não chega a unidades de ns:** os ~40 ns são o próprio `sys.monitoring` despachando para um
callback que **não faz nada** (medido) — piso do PEP 669. Nem a injeção de bytecode resolve: ela ainda
emite um `CALL` Python por evento (dezenas de ns). Nenhum mecanismo por-evento do CPython chega a
dígitos únicos; a ação por-evento mais barata do interpretador já custa ~30–50 ns.

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

## CAPÍTULO 4 — FASE 3: re-execução  **[degraus 1–2 implementados]**

Um programa é função determinística dos seus inputs não-determinísticos (I/O, relógio, aleatoriedade,
env, escalonamento). Gravando **apenas** essas fontes nas bordas (barato), reexecuta-se o código
substituindo cada fonte pelo valor gravado — o bug se repete bit a bit.

**Degrau 1 — repro raso (`_repro.py`, `flight repro`).** Reconstrói os argumentos do frame do crash a
partir do grafo de objetos e gera um `repro_bug.py` autocontido. O reconstrutor grafo→código preserva
**aliasing e ciclos** (contêineres mutáveis criados vazios e depois preenchidos), embute a fonte,
resolve a função (`Class.method` inclusive) e a chama com os locais que casam com a assinatura
(`inspect.signature`). Objetos opacos viram stubs de atributos (⇒ `approximate`). **Verificação:** roda o
script em subprocesso e só rotula `verified` se reproduzir a mesma exceção. Meta honesta: bugs que
dependem de argumentos/estado local.

**Degrau 2 — replay determinístico (`_nondet.py`, `flight.deterministic()`/`replay()`).** Interpõe uma
allowlist de fronteiras por **atributo de módulo** (time.*, random.*, uuid4, os.urandom/getpid/getenv,
secrets.*): no record, cada chamada grava seu resultado num bloco **NONDET**; no replay, devolve os
valores gravados em ordem. Uma **guarda de reentrância** grava só a chamada mais externa (uuid4 chama
os.urandom internamente ⇒ uma entrada). `ReplayDivergence` dispara quando o `source` da próxima entrada
não bate com a chamada — i.e., o fluxo divergiu (por si só um sinal forte). O codec de valores vive em
Python (float→repr, bytes→hex, dict→JSON); o formato só persiste strings.

**Convergência.** `deterministic()` que termina por exceção grava frames + grafo **e** a fita NONDET no
mesmo arquivo (`dump_crash` com a fita). O `repro` então tece a fita: re-invoca a função sob
`replay_tape` até a fita levar à falha gravada (cobre loops que quebram na iteração N) — reproduz um
**crash flaky de tempo/aleatoriedade deterministicamente**.

**Degrau 3 — threads (pesquisa, não implementado).** `sys.setswitchinterval` alto + gravação da ordem de
PY_START entre threads (temos os tstamps lógicos) + escalonador cooperativo no replay. Declarado
honestamente: replay garantido só **single-thread / single-loop asyncio**. Interposição de
arquivos/sockets/subprocess reconhecida porém **estagiada** (estado maior/stateful) — a classe
relógio/aleatoriedade/uuid já está coberta.

## CAPÍTULO 5 — Ordem de ataque

```
Fase 0  ✅  repo, maturin, ring contando eventos, round-trip do formato, benchmark baseline.
Fase 1  ✅  excepthooks + captura de frames/locals + serializador de grafo + fontes + scrubbing.
Fase 2  ✅  with flight.record(): MUTATION via LINE-diff + watch(); timeline (history/who/state_at).
Fase 1.5 ✅ Viewer Textual (frames→locais→grafo→código inline→ring→timeline) sobre o reader.
Fase 3   ✅ degrau 1 (repro verificado) + degrau 2 (replay determinístico) + convergência.
            degrau 3 (threads) = pesquisa; arquivos/sockets estagiados.
Fase 4   ✅ 4a: arquivos (read/readline/readinto/iter) + pipes (os.read) + subprocess (run/check_output)
            na fita; replay offline (escritas engolidas); hash-of-rest (>io_hash_above → len+digest,
            verificado na fonte viva); asyncio = ordem de conclusão gravada+verificada.
            4b: sockets (recv/recv_into); ORDEM de aquisição de locks entre threads gravada+IMPOSTA no
            replay (threads numeradas por início, cursores por-thread na fita, filtro p/ locks internos
            do runtime, timeout→ReplayDivergence). Fora: corridas sem lock, multiprocessing, per-await.
Fase 5   ✅ depurador reverso: engine time-travel (_timetravel.py) sobre a Recording (cursor step
            fwd/back, state() reconstrói locais+contêineres, breakpoint-no-passado find_first("x>100"),
            parser de condição seguro, line-bp/watchpoint continue_forward/back); DAP (_dap.py) com
            supportsStepBack (stepBack/reverseContinue → VS Code/PyCharm); CLI `flight debug [--find|--list]`.
            Por-linha; sub-linha = bytecode nativo (§3.2), futuro.
Fase 6   ✅ debugging por comparação. flight diff (_diff.py): alinha 2 gravações posição-a-posição, 1ª
            divergência no eixo mais rico (MUTATION timeline / NONDET tape / event ring); source-mismatch
            = fluxo ramificou; CLI sai 1 se divergem. Delta-debug (_ddmin.py): ddmin de Zeller (puro) +
            minimize_tape/flight.minimize — replaya neutralizando valores até o conjunto mínimo que ainda
            falha; neutralização que ramifica → ReplayDivergence → "não reproduz" → valor mantido.
Fase 7   ✅ inteligência. flight explain (_explain.py): resumo heurístico offline (exceção+frame+locais
            suspeitos, palpite p/ ZeroDiv/Index/Key/None+attr) + prompt LLM; provider injetável, --llm
            opt-in (Anthropic), falha nunca quebra (P1). repro --pytest (_repro.py): teste de regressão
            commitável (pytest.raises) + auto-verifica via __main__. Query semântica len(x) op N no
            _timetravel (nº de chaves distintas ao longo da timeline). fingerprint (_fingerprint.py): hash
            estável de exceções + (qualname,basename,offset) por frame + kinds dos locais = dedup Sentry.
Fase 8   ✅ produção. TUDO Python puro (correlação na fita NONDET; dump ring-only ganha correlação via
            dump_nondet; granularidade retunada ao vivo com sys.monitoring.set_events). Governador SLO
            (_governor.py): OverheadLadder puro (histerese, lines→returns→calls) + Governor amostra a vazão
            de eventos num thread de fundo, estima overhead = eventos×per_event_ns÷intervalo, desce quando
            vira loop quente (nunca abaixo de calls), sobe ao esfriar (limitado ao baseline pedido);
            install(overhead_slo=) / run --slo. Supervisor (_daemon.py): thread grava checkpoints atômicos
            (temp+rename) do ring; processo supervisor compartilha um pipe — shutdown limpo manda 1 byte e
            descarta o checkpoint, morte incatável (SIGKILL/OOM/segfault) fecha o pipe → EOF → promove o
            último checkpoint a flight-killed-*.flight; start_daemon() / run --daemon. Correlação
            (_correlation.py): TraceContext W3C (traceparent/tracestate), lido de header/OTel-ao-vivo/env;
            correlate() carimba, link() referencia upstream, Flight.correlation() lê de volta, trace_graph/
            `flight trace` agrupa por trace_id → grafo cross-service. Honesto: overhead single-thread
            (superestima em N cores = seguro p/ SLO); checkpoint periódico (kill duro perde ≤1 intervalo);
            supervisor-sobre-checkpoint, não ring em shm lido ao vivo (futuro). Testes: test_production.py
            (28, incl. SIGKILL num filho real recuperando o black box).
Fase 9   ✅ ecossistema (completa, 6 de 6). Plugin pytest (_pytest.py, entry point pytest11): --flight,
            hookwrapper em pytest_runtest_call, na falha (outcome.excinfo) escreve .flight completo nomeado
            pelo node id; nunca muda o resultado (P1). Cripto (_crypto.py): envelope FLGTENC1|salt|nonce|
            ct+tag, KDF scrypt (stdlib, maxmem explícito), AEAD AES-256-GCM (cryptography, extra [crypto]);
            sem o pacote → CryptoUnavailable. Viewer WASM: flight-format ganhou features c-zstd (encoder+
            decoder C, default) / pure-zstd (decoder ruzstd, Rust puro); writer só em c-zstd; flight-reader
            propaga as features; crate flight-wasm (cdylib, workspace próprio) expõe ABI C cru alloc/parse/
            dealloc (JSON com prefixo u32 de tamanho), buildado p/ wasm32 sem wasm-bindgen; scripts/
            build-wasm.sh embute o .wasm base64 em viewer-wasm/index.html (offline, file://). Middleware
            (_web.py): FlightWSGI/FlightASGI, .flight por 500 com trace da request passado por-request
            (capture ganhou arg correlation, thread-safe), agnóstico de framework. flight ci (_ci.py) +
            .github/actions/flight: comentário Markdown de causa-raiz (reusa explain+fingerprint). Recorders
            recorders/go + recorders/node: mesmo formato, msgpack à mão + frame zstd "stored" (blocos raw =
            zstd válido sem compressor) → zero deps, lidos pelo reader Rust. Testes: test_ecosystem.py
            (plugin/crypto/middleware/ci) + test_polyglot.py (Go/Node/WASM; pulam sem o toolchain).
Fase 10  ✅ moonshot: what-if. flight.what_if(path, fn, overrides) (_whatif.py): dois replay_tape do mesmo
            fn sobre a MESMA fita (fresh tape por run — replay avança os cursores) — baseline reproduz o
            gravado, contrafactual roda com sys.settrace que na linha alvo faz frame.f_locals[var]=value
            (write-through PEP 667, Python 3.13+; senão o render avisa). Outcome = returned | exception |
            diverged (ReplayDivergence = a mudança sai da fita, ex.: chama random() a mais) | não-aplicado
            (reportado). Override(var,value,line,qualname?,nth) aplicado ANTES da linha (mire a linha que
            USA o valor). P1: exceção do próprio contrafactual capturada, não levantada. API (como
            minimize). Testes: test_whatif.py (8).
Fase 5   🔜 depurador reverso: step-backward + breakpoint no passado sobre state_at(seq);
            bytecode nativo (§3.2) p/ sub-linha; exposição via DAP (VS Code/PyCharm).
Fase 6   🔜 flight diff (primeira divergência) + delta debugging (ddmin sobre a fita).
Fase 7   🔜 inteligência: flight explain (LLM), repro --pytest, query semântica, dedup frame+estado.
Fase 8   ✅ produção (ver bloco acima): governador de overhead (SLO), supervisor + flush no crash
            (sobrevive SIGKILL/OOM), correlação distribuída (W3C traceparent / OpenTelemetry) + flight trace.
Fase 9   ✅ ecossistema (ver bloco acima): viewer WASM offline, plugin pytest, flight ci + GitHub Action,
            middleware WSGI/ASGI, recorders Go+Node no mesmo formato, cripto em repouso.
Fase 10  ✅ moonshot: what-if debugging (ver bloco acima): flight.what_if, override de local vivo (PEP 667)
            + replay sobre a fita → baseline vs contrafactual.
```

Detalhamento de cada fase futura: VISION.md §5.6.
