# Projeto Flight — Gravador de Voo para Python
### Documento Mestre do Projeto (v1.0)

> **Missão em uma frase:** quando um programa Python morre, o desenvolvedor não deve receber um
> traceback e boa sorte — deve receber a caixa-preta completa do voo, navegável, compartilhável e, no
> futuro, reexecutável no tempo.

Este documento é a fonte da verdade do projeto. Ele existe para que a visão nunca se perca, para que
cada decisão técnica tenha um "porquê" registrado, e para que qualquer fase possa ser retomada meses
depois sem confusão. O par técnico deste documento é [TECHNICAL.md](TECHNICAL.md).

---

## PARTE I — A VISÃO

### 1. O problema

Hoje, quando um programa Python falha em produção (ou até localmente), o desenvolvedor recebe um
traceback: a lista de chamadas e a linha do erro. Isso responde **onde** o programa morreu, mas quase
nunca responde **por quê**:

- Quais eram os valores das variáveis em *cada* frame, não só uma pista no último?
- Como aquela variável virou `None`? Quem escreveu nela pela última vez?
- Que caminho o código percorreu nos instantes antes da morte?
- Como eu reproduzo isso na minha máquina?

O ciclo real de debug é: adicionar prints/logs → tentar reproduzir → falhar em reproduzir → adicionar
mais logs → esperar acontecer de novo em produção → repetir. Dias perdidos. O Flight ataca exatamente
esse ciclo.

### 2. A tese

Três apostas fundamentam o projeto:

1. **Técnica:** o `sys.monitoring` (PEP 669, Python 3.12+) tornou viável, pela primeira vez,
   instrumentar a execução do CPython com overhead baixo o suficiente para uso em produção. Um núcleo
   em Rust (via PyO3) mantém o custo de cada evento na casa de nanossegundos.
2. **De produto:** ferramenta de debug é 50% engine e 50% experiência de leitura. A lição do
   `rr` → Pernosco: a mesma tecnologia, com um viewer excelente, saiu de "curiosidade acadêmica" para
   "produto indispensável". Por isso o viewer é uma fase de primeira classe, não um extra.
3. **De adoção:** o arquivo `.flight` compartilhável é o vetor viral. Cada arquivo mandado no Slack
   ("abre isso aí que você vê tudo") recruta um usuário novo. O bug report perfeito se escreve sozinho.

### 3. O que o Flight é — e o que não é

**É:** um gravador post-mortem e de escopo delimitado, com um viewer de primeira linha, evoluindo para
time-travel debugging.

**Não é (e não deve virar):**
- Um APM/observabilidade tipo Datadog (não agregamos métricas de frota; capturamos voos individuais em
  profundidade).
- Um debugger interativo ao vivo (pdb já existe; nosso valor é o *depois*).
- Um profiler (medimos o quê aconteceu, não quanto tempo levou — ainda que timestamps existam no
  formato).

Sempre que uma feature parecer atraente, pergunte: "isso ajuda alguém a entender um voo que já
aconteceu?" Se não, está fora.

### 4. Princípios invioláveis (decorar estes cinco)

**P1 — Primum non nocere.** O gravador NUNCA pode derrubar, travar ou corromper o programa do usuário.
Todo hook engole os próprios erros e degrada para gravação parcial. Um `.flight` incompleto é
aceitável; um crash causado pelo Flight, jamais. Isso implica: `catch_unwind` em todo entry point
Rust, timeouts em serialização, limites de memória rígidos.

**P2 — Overhead honesto e limitado.** Modo caixa-preta (Fase 1): alvo < 5% de overhead, teto absoluto
10%. Modo gravação completa (Fase 2): alvo < 3x, aceitável até 5x, sempre em escopo delimitado e
explícito. Se uma feature estoura o orçamento, ela vira opt-in ou morre.

**P3 — O formato `.flight` é a espinha dorsal.** Engine e viewer só se falam através dele. O formato
nasce já prevendo os eventos das fases futuras (mutação, timeline), para que o viewer da Fase 1.5 ganhe
superpoderes na Fase 2 sem reescrita. Versionado desde o dia 1; leitores toleram campos desconhecidos.

**P4 — Cada fase é um produto útil sozinho.** Se o projeto parar na Fase 1.5, ele ainda é a melhor
ferramenta post-mortem do ecossistema Python. Nenhuma fase depende do sucesso da seguinte para
justificar sua existência.

**P5 — Privacidade por design.** O `.flight` captura valores reais de variáveis — potencialmente
senhas, tokens, dados pessoais. Redação (scrubbing) configurável de campos sensíveis existe desde a
Fase 1, não como remendo posterior. Padrões óbvios (`password`, `token`, `secret`, `authorization`)
vêm redigidos por default.

### 5. Roadmap em um olhar

| Fase | Nome | Entrega central | Status |
|------|------|-----------------|--------|
| 0 | Fundação | Repo, CI, formato `.flight` v1, esqueleto Rust+Python funcionando | ✅ **concluída** |
| 1 | Caixa-preta | Captura automática de exceções: todos os frames + locals + grafo de objetos + fontes | ✅ **concluída** |
| 1.5 | Viewer | TUI navegável: frames → locals → grafo de objetos → código com valores inline | ✅ **concluída** |
| 2 | Time-travel de escopo | `with flight.record():` grava escritas de estado; histórico por variável; "quem mutou" | ✅ **concluída** |
| 3 | Re-execução | Repro automático verificado; replay determinístico (time/random/uuid/…) | ✅ **degraus 1–2** (degrau 3, threads: pesquisa) |

### 5.1 Definição de "pronto" da Fase 0

- Workspace Rust (`flight-format`, `flight-reader`, `flight-core`) + pacote Python `flight`, compilando
  via maturin.
- Formato `.flight` v1: header, blocos tipados (msgpack + zstd), footer opcional, **round-trip testado**
  e **tolerância a truncamento testada byte a byte**.
- Ring buffer lock-free por thread + relógio lógico, alimentado por `sys.monitoring`, **contando e
  ordenando eventos** entre threads.
- `install()`/`uninstall()`, `excepthook`, `capture()` manual, e CLI `python -m flight run|inspect`.
- Benchmark de overhead baseline (`scripts/bench.py`) — número honesto, não promessa.

### 5.2 Definição de "pronto" da Fase 1 (o que esta entrega cumpre)

- No crash (excepthook) ou via `capture()`, o `.flight` passa a carregar, além de META + EVENT_RING:
  **EXCEPTION** (cadeia `__cause__`/`__context__`), **FRAME** (todos os frames, do crash para fora,
  com os locals), **OBJECT** (grafo de objetos serializado) e **SOURCE** (fonte de cada arquivo).
- **Serializador de grafo** com: preservação de **identidade/aliasing** (o MESMO objeto em dois frames
  = um nó só), segurança contra **ciclos**, limites por contêiner/string, limite de **profundidade**,
  **orçamento global** de tempo (250 ms) e bytes (20 MB), `safe_repr` à prova de `__repr__` hostil, e
  tipos opacos (módulos/classes/funções) como folhas.
- **Scrubbing (P5)** de valores sensíveis por nome (chaves de dict, atributos e **nomes de locais**).
- **Adaptadores** plugáveis (numpy/pandas) resolvidos por qualname, sem virar dependência.
- CLI `inspect` enriquecido: exceção, frames com locais renderizados e marcação de aliasing (`↔`).
- Reader com API de consulta: `exceptions()`, `frames()`, `objects()`, `object_map()`, `aliases()`.
- 34 testes Rust + 36 testes Python, todos verdes.

O que **não** está na Fase 1 (é 1.5): o viewer TUI navegável. Os dados já estão todos no `.flight`.

### 5.3 Definição de "pronto" da Fase 2 (o que esta entrega cumpre)

- `with flight.record():` — escopo explícito que grava **escritas de estado** num bloco MUTATION e
  fecha um `.flight` limpo na saída (mesmo se uma exceção sair do bloco).
- Captura **robusta e à prova de versão**, sem cirurgia de bytecode: por evento LINE dentro do escopo,
  (a) **diff dos locais** do frame → rebind de variáveis, e (b) **diff de snapshot** de objetos sob
  `watch(obj)` → escritas em contêineres/atributos, sem substituir o objeto (não quebra `type()`).
- **Timeline / time-travel** sobre o log: `Recording.history(nome)` (evolução de uma variável),
  `who_mutated(nome)` ("quem mutou este dict"), `state_at(seq)` (reconstruir os locais num instante —
  event sourcing). CLI: `flight timeline [--var|--who]`.
- **Scrubbing (P5)** aplicado a nomes de locais e chaves/atributos observados; **opt-in e delimitado**
  (só paga custo em torno do código investigado, P2); cap de mutações para não crescer sem limite.
- 36 testes Rust + 50 testes Python, todos verdes.

Granularidade: a captura é por linha. Como o evento LINE dispara *antes* da linha rodar, a mudança
detectada é atribuída à linha anterior executada — a que de fato fez a escrita — dando **atribuição de
linha exata**; e a última escrita de um frame (sem evento LINE seguinte) é recuperada no
PY_RETURN/PY_UNWIND, então nada é perdido. Várias escritas na mesma linha física compartilham essa
linha. Granularidade por instrução via instrumentação nativa de bytecode é o passo futuro
(TECHNICAL.md §3.2, opção A). O que **não** está na Fase 2 (é Fase 3): replay determinístico.

### 5.4 Definição de "pronto" da Fase 1.5 (o que esta entrega cumpre)

- Viewer TUI ([Textual](https://textual.textualize.io)) — `flight view arquivo.flight` — que **só fala
  com a API do reader** (nunca bytes crus, P3).
- Painel esquerdo: `Tree` de **frames → locais → grafo de objetos** com expansão *lazy* (abre `.flight`
  grande instantaneamente); objetos com aliasing marcados com `↔`.
- Painéis (abas): **Source** (código do frame com a linha do crash marcada e **valores inline** nos
  identificadores presentes nos locais — o recurso dos 5 segundos), **Detail** (tipo/valor/aliasing do
  objeto selecionado), **Exception** (cadeia), **Events** (o ring — que caminho o código percorreu), e
  **Timeline** (o log de mutações da Fase 2, quando presente).
- Ação `a`: mostra onde o MESMO objeto aparece (aliases). Abre também arquivos só-ring (Fase 0) e de
  escopo (Fase 2).
- Lógica de render (valores inline, índice de aliases, janela de código) isolada em `_viewer_model`
  (testável sem terminal); o app é uma casca fina, testada *headless* via o `Pilot` do Textual.
- Textual é dependência **opcional** (`pip install flight-recorder[viewer]`); a CLI degrada com
  mensagem clara se ausente.

### 5.5 Definição de "pronto" da Fase 3 (o que esta entrega cumpre)

Os três degraus do guia (§4), entregues em ordem:

- **Degrau 1 — repro raso (`flight repro`).** De um crash `.flight`, gera um `repro_bug.py`
  **autocontido e verificado**: reconstrói os argumentos do frame do crash a partir do grafo de objetos
  (com aliasing/ciclos preservados e stubs para objetos opacos), embute a fonte, chama a função e
  confere a exceção — rodando em subprocesso, só rotula "verified" se de fato reproduz. Bugs que
  dependem de argumentos/estado local (uma classe enorme de bugs de lógica).
- **Degrau 2 — replay determinístico (`with flight.deterministic()` / `flight.replay()`).** Um programa
  é função determinística dos seus inputs não-determinísticos; gravamos **só** essas fontes
  (time/monotonic/perf_counter, random.*, uuid4, os.urandom/getpid/getenv, secrets.*) num bloco NONDET,
  e no replay devolvemos os valores gravados em ordem — a execução se repete bit a bit. `ReplayDivergence`
  aponta o passo exato em que o fluxo divergiu. Modelo do `rr` no nível de APIs Python. Guarda de
  reentrância grava só a chamada mais externa (uuid4 usa os.urandom internamente → uma entrada, não duas).
- **Integração.** Um crash dentro de `deterministic()` grava frames + grafo **e** a fita NONDET no mesmo
  arquivo; `flight repro` então tece a fita no script gerado (re-invocando até a fita levar à falha
  gravada) — **reproduzindo um crash flaky de tempo/aleatoriedade de forma determinística**.
- **Degrau 3 (threads):** pesquisa. Honestamente: replay garantido apenas **single-thread /
  single-loop asyncio**. Interposição de arquivos/sockets/subprocess reconhecida porém estagiada
  (estado maior); a classe relógio/aleatoriedade/uuid — testes flaky, "falha 1% das vezes" — está coberta.
- 38 testes Rust + 90 testes Python, todos verdes.

---

## PARTE II — ARQUITETURA GERAL

### 6. Visão dos componentes

```
┌──────────────────────────── Processo do usuário ────────────────────────────┐
│  Código do usuário (inalterado)                                              │
│        │ eventos via sys.monitoring (PY_START, LINE, RAISE, ...)             │
│        ▼                                                                      │
│  ┌───────────────────┐        ┌──────────────────────────┐                  │
│  │ flight (Python)    │──────► │ flight_core (Rust)        │                  │
│  │ - API pública      │        │ - ring buffer lock-free   │                  │
│  │ - install()/hook   │        │ - serializador / writer   │                  │
│  │ - config/scrub     │        │ - writer do .flight (zstd)│                  │
│  └───────────────────┘        └──────────────────────────┘                  │
│                                       │ no crash: flush                       │
└───────────────────────────────────────┼──────────────────────────────────────┘
                                         ▼
                                   crash.flight
                                         │
                    ┌────────────────────┴────────────────────┐
                    ▼                                          ▼
          flight inspect (CLI)                    flight view (TUI, Fase 1.5)
                    └──── ambos leem via flight_reader (Rust) ────┘
```

Divisão de responsabilidades:

- **`flight` (pacote Python):** API pública, ergonomia, configuração, hook de `sys.excepthook` /
  `sys.monitoring`. Fino de propósito — quase tudo delega para o core.
- **`flight-core` (crate Rust, via PyO3, módulo `flight._core`):** o caminho quente. Recebe eventos,
  mantém o ring buffer, escreve o arquivo. Aqui mora a performance e a robustez (P1 e P2).
- **`flight-reader` (crate Rust):** parser do `.flight`, tolerante a truncamento e a blocos
  desconhecidos. Usado pela CLI e, futuramente, pelo viewer.
- **`flight-format` (crate Rust):** a definição do formato — blocos, eventos, writer. A espinha (P3).

### 7. Por que `sys.monitoring` e não `sys.settrace`

`sys.settrace` (o mecanismo antigo) impõe overhead brutal (10–30x) porque chama um callback Python para
cada linha de cada frame, sempre. O `sys.monitoring` (3.12+) permite: registrar interesse apenas em
eventos específicos, desativar eventos por código-objeto individual (`DISABLE`), e callbacks muito mais
baratos.

**Decisão:** Python mínimo suportado = **3.12**.

### 8. O formato `.flight`

Ver o documento dedicado: **[docs/FORMAT.md](docs/FORMAT.md)**. Resumo: sequência de blocos
autocontidos, cada um `tipo | tamanho | zstd(msgpack)`, com header sniffável e footer (índice)
opcional. O leitor funciona com ou sem footer; arquivo truncado é lido até onde der (coerente com P1).

**Regra de ouro do formato:** leitores novos leem arquivos velhos; leitores velhos pulam blocos novos.
Nunca quebrar isso.

### 9. Serialização de objetos (Fase 1)

Não dá para "pickle tudo": objetos podem ser gigantes, cíclicos, não-serializáveis, ou ter `__repr__`
que executa código (perigoso sob P1). A estratégia — grafo com identidade, tipos nativos por valor com
truncamento, contêineres com limites, `safe_repr`, adaptadores plugáveis (ndarray/DataFrame),
orçamento global de tempo/bytes, e scrubbing (P5) — está detalhada em [TECHNICAL.md](TECHNICAL.md) §1.4.
É trabalho da Fase 1.

### 10. O ring buffer

Um buffer circular em Rust, lock-free, de tamanho fixo (default: 4.096 eventos por thread). Cada evento
registra código-objeto id, linha, timestamp lógico e thread id — 24 bytes, cabe em cache line. **Não**
serializa locals a cada linha (custo proibitivo) — locals completos só no momento do crash (Fase 1). Um
ring por thread, mesclados por timestamp lógico na escrita. Detalhes em
[TECHNICAL.md](TECHNICAL.md) §1.5.
