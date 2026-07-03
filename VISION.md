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
| 4 | Fidelidade total de replay | Fechar o degrau 3: interpor arquivos/sockets/subprocess + **ordem** de locks/tasks (threads e asyncio) → replay multi-thread bit a bit | ✅ **concluída** (4a arquivos/pipes/subprocess + asyncio; 4b sockets + ordem de locks entre threads) |
| 5 | Depurador reverso de verdade | *Step-backward* + "breakpoint no passado" sobre `state_at(seq)`; exposição via **DAP** (VS Code / PyCharm); granularidade sub-linha via bytecode nativo | ✅ **concluída** (engine + DAP com `supportsStepBack`; sub-linha via bytecode = futuro) |
| 6 | Debugging por comparação | `flight diff a.flight b.flight` (primeira divergência) + **delta debugging** (ddmin sobre a fita → repro mínimo) | ✅ **concluída** (`flight diff` + `flight.minimize` via ddmin) |
| 7 | Camada de inteligência | `flight explain` (causa-raiz + patch por LLM), `flight repro --pytest`, query semântica na timeline, dedup por frame+estado | ✅ **concluída** (explain heurístico+prompt LLM, repro --pytest, `len(x)>N`, fingerprint) |
| 8 | Caixa-preta de produção | Governador adaptativo de overhead (SLO), daemon always-on + flush no crash (sobrevive a SIGKILL/OOM), correlação distribuída (OpenTelemetry) | ✅ **concluída** (governador SLO retunando a granularidade ao vivo; supervisor que promove o último checkpoint após `kill -9`; correlação W3C `traceparent` + `flight trace`) |
| 9 | Laço viral e ecossistema | Viewer no browser (reader Rust → WASM), plugin pytest, GitHub Action, middleware Django/FastAPI/Flask, recorders cross-language, cripto em repouso | ✅ **concluída** (viewer WASM offline, plugin `pytest --flight`, `flight ci` + GitHub Action, middleware WSGI/ASGI, recorders Go+Node no mesmo formato, cripto AES-256-GCM) |
| 10 | Moonshots | *What-if debugging*: editar um valor no passado e re-executar dali sobre a fita determinística — resultado contrafactual | ✅ **concluída** (`flight.what_if`: baseline vs contrafactual sobre a fita, override de local ao vivo via PEP 667) |

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

### 5.6 Roadmap futuro — Fases 4–10

O norte destas fases: **fidelidade → experiência → inteligência → alcance**. Cada uma continua obedecendo
os cinco invioláveis (P1–P5) e a regra de que **toda fase é útil sozinha** (P4). O que segue é o contrato
de cada fase — o "pronto" que vamos perseguir.

**Fase 4 — Fidelidade total de replay (fechar o degrau 3).** É a base de tudo. Interpor as fronteiras que
faltam — arquivos, sockets, subprocess — **e o escalonamento**. O truque decisivo: gravar a **ordem** de
aquisição de locks e de retomada de tasks (threads e asyncio), não os dados internos do escalonador. Um
programa é função determinística das suas entradas *e da ordem em que o mundo respondeu*; grave essa ordem
e o replay multi-thread passa a repetir bit a bit. Entregável: `flight.deterministic()` cobrindo o **crash
flaky de concorrência** — o pesadelo #1 de todo dev. Parte difícil honesta: I/O grande infla o arquivo →
provavelmente um modo "grava só o que foi lido, com hash do resto" para conciliar fidelidade e tamanho.

> **Fatia 4a — entregue.** `flight.deterministic()` agora grava, além dos escalares (relógio/random/uuid),
> **o que o código leu**: arquivos (texto/binário, `read`/`readline`/`readinto`/iteração), pipes (`os.read`)
> e a saída de subprocessos (`subprocess.run`/`check_output`) — cada leitura é uma entrada na mesma fita
> `seq`-ordenada (`_io.py`), com **numeração de canal por ordem de `open`** para não cruzar arquivos
> interleavados. `flight.replay()` reproduz **offline**: as leituras vêm da fita e as **escritas são
> engolidas** (nenhum efeito colateral real no disco). O modo **"grava só o que foi lido, com hash do
> resto"** está implementado: leituras acima de `io_hash_above` bytes viram *comprimento + digest BLAKE2b*
> (arquivo minúsculo), e no replay a fonte viva é relida e **verificada** contra o digest;
> `io_hash_above=0` inlina tudo para replay 100% offline. Para **asyncio**, gravamos a **ordem de conclusão
> das tasks** e a **verificamos** no replay (`_asyncio.py`): como o determinismo vem de reproduzir
> tempo+I/O, a verificação detecta e aponta qualquer divergência de escalonamento residual (detector, ainda
> não *impositor*).
>
> **Fatia 4b — entregue.** Sockets (`recv`/`recv_into`) entram na fita como os demais reads (offline). E o
> **núcleo research** foi implementado: **ordem de aquisição de locks entre threads** (`_threads.py`).
> Threads são numeradas por ordem de início (`_flight_channel`; a thread do escopo = canal 0), e **cada
> thread reproduz suas próprias chamadas de fronteira na sua própria trilha** da fita (cursores por-thread
> em `Tape`, com o append da fita protegido por lock e a guarda de reentrância por-thread) — chamadas
> concorrentes e não-sincronizadas (dois threads lendo o relógio) nunca disputam uma ordem global. A ordem
> que **importa** — aquisição de locks — é gravada como uma sequência de canais e **imposta** no replay:
> cada thread espera sua vez (a cabeça da sequência) antes de a aquisição prosseguir, reproduzindo o
> **agendamento de locks** bit a bit (o clássico bug flaky de "qual thread ganhou"). **Honestidade:** só
> locks criados **dentro** do escopo pelo código do usuário são rastreados (locks internos do runtime —
> `threading`/`queue`/… — ficam intactos por um filtro de módulo-chamador, senão a própria sincronização do
> interpretador travaria); aquisições **não-bloqueantes/com timeout** não são ordenadas; e **corridas de
> dados sobre estado não-travado** ficam fora (genuinamente fora de qualquer record/replay baseado em
> locks). Um timeout de segurança transforma um deadlock de replay em `ReplayDivergence`, nunca num
> travamento (P1). Multiprocessing e a ordenação fina por-`await` do asyncio seguem como trabalho futuro.

**Fase 5 — O depurador reverso de verdade.** Já temos `state_at(seq)` (event sourcing); falta a
*experiência*: **step-backward**. Um viewer/DAP onde se anda para trás no tempo, coloca um "breakpoint no
passado" ("pare quando `running` passou de 100") e a UI reconstrói os locais naquele instante. Combinado
com a **instrumentação de bytecode nativa** que o TECHNICAL §3.2 já documenta como futuro, isso dá
granularidade **sub-linha** e faz do flight um concorrente direto do `rr`/Pernosco — mas para Python e com
o grafo de objetos junto. Exposição via **DAP (Debug Adapter Protocol)** ⇒ VS Code e PyCharm de graça.

> **Entregue.** O motor de time-travel (`_timetravel.py`) é lógica pura sobre uma `Recording`: um cursor que
> anda **para trás** e para frente pelas escritas de estado, `state()` reconstrói locais **e** o conteúdo de
> contêineres no ponto do cursor (event sourcing), e o **"breakpoint no passado"** é uma busca na timeline —
> `find_first("running > 100")` pula para a escrita que primeiro satisfez a condição (parser de condição
> seguro, sem `eval`; comparações inválidas nunca quebram a sessão, P1). Breakpoints de linha e watchpoints
> com `continue_forward`/`continue_back`. A sessão começa no **fim** (postura post-mortem) e anda para trás.
> A exposição via **DAP** (`_dap.py`) anuncia `supportsStepBack` — então **VS Code e PyCharm mostram os
> botões "Step Back"/"Reverse" e mandam `stepBack`/`reverseContinue`** de graça; o adaptador é read-only
> sobre o `.flight` (as variáveis vêm da reconstrução no cursor, `evaluate` é um REPL com `find running >
> 100`). `DebugAdapter.handle` é puro (dict→dicts), testado sem editor; `serve()` adiciona o enquadramento
> Content-Length sobre stdio. CLI: `flight debug arquivo.flight` (servidor DAP) ou `--find "running > 100"` /
> `--list` para responder na linha de comando. **Escopo honesto:** opera sobre gravações de escopo (Fase 2,
> com a timeline de MUTATION); granularidade por-linha (a sub-linha depende do bytecode nativo, fase futura).

**Fase 6 — Debugging por comparação: `flight diff` + minimização.** Duas capacidades que multiplicam o que
já existe:
- `flight diff run_ok.flight run_falha.flight` — compara duas gravações e aponta a **primeira mutação/evento
  onde divergiram**. Mata "funciona na minha máquina" e testes flaky de um jeito que traceback nenhum
  consegue.
- **Delta debugging automático** — dado um crash com a fita determinística, encolher as entradas gravadas
  até o reprodutor mínimo (**ddmin** sobre a tape): "seu bug precisa só destes 3 valores dos 500 gravados".

> **Entregue.** `flight diff a.flight b.flight` (`_diff.py`) alinha duas gravações **posição a posição** e
> reporta a primeira divergência, escolhendo o eixo mais rico que ambas compartilham: **timeline de MUTATION**
> (a primeira escrita cujo alvo/valor difere), **fita NONDET** (a primeira chamada de fronteira que respondeu
> diferente — mismatch de *source* = fluxo de controle ramificou, a raiz de um teste flaky) ou o **ring de
> eventos**. CLI `flight diff` sai com código 1 quando divergem (como o `diff(1)`), útil em CI. O **delta
> debugging** (`_ddmin.py`) é o `ddmin` clássico de Zeller (puro e testado à parte) ligado ao motor de
> replay: `flight.minimize(path, fn)` reexecuta `fn` sob a fita com cada vez mais valores gravados
> substituídos por um **default neutro**, mantendo só as reduções que ainda reproduzem a falha, até sobrar o
> conjunto mínimo de valores *load-bearing* — "seu bug precisa só destes N valores". Neutralizar um valor que
> muda o fluxo de controle faz o replay divergir, o que o predicado lê como "não reproduziu", então esse
> valor é corretamente mantido. Predicado padrão = ainda levanta exceção; customizável. **Escopo honesto:**
> `minimize` é API Python (precisa da `fn` a reexecutar, como o `replay`); a integração de linha de comando
> (reconstruir a `fn` via `repro`) fica para depois.

**Fase 7 — A camada de inteligência (o diferencial de 2026).** Um `.flight` é o **contexto estruturado
perfeito** para um LLM — infinitamente melhor que um traceback solto. Sobre isso:
- `flight explain crash.flight` → causa-raiz em linguagem natural + patch sugerido, alimentando o modelo
  com frames + grafo + ring + source (tudo já consultável pelo reader).
- `flight repro --pytest` → transforma o repro verificado num **caso de teste de regressão commitável**,
  com as entradas do crash congeladas. O bug report vira proteção permanente.
- **Query semântica** sobre a timeline: "quando `cache` passou de 100 entradas?" rodando sobre as mutações.
- **Deduplicação** estilo Sentry, mas por **frame comum + estado**, não só por stack.

> **Entregue.** `flight explain crash.flight` (`_explain.py`) tem duas metades: (1) um **resumo heurístico
> de causa-raiz** determinístico e **offline** — identifica a exceção, o frame do crash, e os locais
> suspeitos (contêiner vazio, `None`, zero) com um palpite dirigido para os casos clássicos
> (ZeroDivisionError → divisor zero; IndexError/KeyError → contêiner vazio indexado; aliasing marcado) — e
> (2) um **prompt pronto para LLM** que empacota cadeia de exceção + stack + janela de código + locais. O
> provider de modelo é uma camada fina e **injetável** (`provider(prompt)→texto`), então o `explain` é útil
> e 100% testado sem chave de API, e *vira* um explicador LLM quando você configura um (Anthropic via
> `anthropic` + `ANTHROPIC_API_KEY`, opt-in com `--llm`); falha de modelo/rede nunca quebra o `explain`
> (P1). `flight repro --pytest` (`_repro.py`) reusa a reconstrução da Fase 3 e emite um **teste de regressão
> commitável** (`def test_regression(): with pytest.raises(...)`) que **também roda como script** (auto-
> verifica pelo `__main__`), então o mesmo arquivo passa no pytest e no verificador por subprocesso.
> **Query semântica** de tamanho de contêiner — `len(cache) > 100` — está no motor de time-travel
> (`find_first`/`find_all`, reconstruindo o nº de chaves distintas ao longo da timeline): "quando `cache`
> passou de 100 entradas?". **Dedup** (`_fingerprint.py`): `flight fingerprint` é um hash curto estável da
> cadeia de exceções + `(qualname, basename, offset-na-função)` de cada frame + os *kinds* dos locais do
> crash — mesmo bug ⇒ mesmo fingerprint (mesmo com linhas deslocadas), bugs diferentes ⇒ fingerprints
> diferentes. **Escopo honesto:** a chamada real ao LLM é opt-in e não testada em CI; as heurísticas,
> prompt, repro --pytest, query e fingerprint são determinísticos e testados.

**Fase 8 — A caixa-preta de produção (deixar ligado de verdade).** Hoje o overhead é honesto porém fixo.
Falta:
- **Governador adaptativo de overhead** — mira um teto rígido (ex.: <3%) e baixa sozinho para call-only
  quando o código gravado vira loop quente. Overhead vira um **SLO**, não uma aposta.
- **Daemon always-on + flush só no crash** — ring em memória compartilhada; num SIGKILL/OOM um processo
  supervisor externo ainda escreve a caixa-preta. Um black box que **sobrevive à morte do avião**.
- **Correlação distribuída** (OpenTelemetry / `traceparent`) — o `.flight` do serviço A referencia o do
  serviço B. Crash cross-service navegável.

> **Entregue (Fase 8).** Pura em Python sobre o que já existia — nenhuma mudança no formato ou no Rust
> (correlação viaja na fita NONDET; o dump ring-only ganha correlação usando `dump_nondet`; a granularidade
> é retunada ao vivo com `sys.monitoring.set_events`). **Governador de overhead como SLO** (`_governor.py`):
> um `OverheadLadder` puro (máquina de estados com histerese, testada com estimativas injetadas) escolhe uma
> **granularidade** — `lines → returns → calls` — e um `Governor` amostra a vazão de eventos do recorder num
> thread de fundo, estima a fração de wall-clock gasta gravando (`eventos × ~65ns ÷ intervalo`) e **desce um
> degrau** quando o código gravado vira loop quente (largando LINE, depois RETURN, nunca abaixo de "quais
> funções rodaram e como desenrolou"), **subindo** de volta quando esfria — sempre limitado à granularidade
> que o usuário pediu. `flight.install(overhead_slo=0.03)` ou `flight run --slo 0.03`. **Supervisor que
> sobrevive à morte do avião** (`_daemon.py`): um thread grava *checkpoints* do ring atomicamente (temp +
> rename) a cada intervalo, e um **processo supervisor** compartilha um pipe com o pai — num desligamento
> limpo o pai manda um byte e o supervisor descarta o checkpoint; numa **morte incatável** (`SIGKILL`, OOM,
> segfault → nenhum Python roda) o SO fecha o pipe, o `read` do supervisor retorna EOF e ele **promove o
> último checkpoint** a `flight-killed-*.flight`. `flight.start_daemon()` ou `flight run --daemon`. **Correlação
> distribuída** (`_correlation.py`): `TraceContext` do W3C Trace Context (`traceparent`/`tracestate`), lido de
> um header explícito, de um span **OpenTelemetry** ao vivo (dependência opcional) ou do ambiente
> (`TRACEPARENT`/`OTEL_SERVICE_NAME`); `flight.correlate(...)` carimba o contexto em todo black box, e
> `flight.link(other)` registra referências cross-service. `Flight.correlation()` lê de volta; `flight trace
> <dir>` agrupa os `.flight` por `trace_id` → o **grafo de crash cross-service** (serviço, exceção-título e
> links). **Escopo honesto:** o governador estima overhead por um custo-por-evento calibrado (single-thread;
> superestima em muitos cores, o que é seguro para um SLO); o checkpoint é periódico, então até `intervalo`
> dos últimos eventos pode se perder num kill duro; é um supervisor-sobre-checkpoint, não (ainda) um ring em
> memória compartilhada lido ao vivo — esse é o refino futuro. Testes: `tests/test_production.py` (28,
> incluindo `SIGKILL` num processo-filho real recuperando o black box).

**Fase 9 — O laço viral e o ecossistema.** O VISION aposta no arquivo compartilhável; falta remover todo o
atrito:
- **Viewer no browser** compilando o `flight-reader` (Rust) para **WASM**. Arrasta o `.flight`, vê a
  experiência da TUI sem instalar nada. Esse é o loop de crescimento.
- **Plugin pytest** — em falha de teste anexa o `.flight` automaticamente; `pytest --flight` regrava as
  falhas com captura total.
- **GitHub Action** — comenta causa-raiz + repro num CI vermelho.
- **Middleware** Django/FastAPI/Flask — um `.flight` por erro 500 com o contexto da request.
- **Recorders cross-language** escrevendo o mesmo formato `.flight` (Node, Go), porque o formato é
  agnóstico de linguagem → grafo de objetos cross-language.
- **Segurança que vira feature:** criptografia opcional em repouso (o arquivo tem valores mesmo pós-scrub),
  para mandar um crash a um fornecedor sem vazar nada.

> **Entregue (Fase 9, completa).** **Plugin pytest** (`_pytest.py`, registrado como entry point
> `pytest11` → auto-descoberto): opt-in por `pytest --flight`, um hookwrapper em `pytest_runtest_call` grava
> cada teste sob o Flight e, na falha, escreve um `.flight` de captura completa nomeado pelo node id
> (`--flight-dir`, default `.flight/`), expondo o caminho no relatório da falha e no resumo do terminal;
> `--flight-lines` (por-linha), `--flight-all` (grava também os que passam). Nunca muda o resultado do teste
> (P1): a gravação é embrulhada, uma falha dentro do Flight só significa "sem black box para esse", nunca um
> teste que falha ou erra à toa. **Criptografia em repouso** (`_crypto.py`): um `.flight` carrega valores
> reais mesmo pós-scrub, então `flight encrypt`/`flight.encrypt_file` sela o arquivo num envelope
> `FLGTENC1 | salt(16) | nonce(12) | ciphertext+tag` — chave derivada da senha por **scrypt** (stdlib
> `hashlib`), AEAD por **AES-256-GCM** (confidencialidade + detecção de adulteração), header ligado como
> associated data; `flight decrypt` reverte, senha errada / truncamento / bit flipado → `DecryptError`. AEAD
> exige o pacote **`cryptography`** (extra `[crypto]`); o KDF e o enquadramento são stdlib e sempre testáveis,
> e sem o pacote as funções levantam um `CryptoUnavailable` claro. **Viewer no browser (WASM)** — o
> `flight-reader` compilado para `wasm32`: como o `zstd` do reader é dep C que não vai p/ wasm, o decode passa
> a usar **`ruzstd`** (Rust puro) atrás da feature `pure-zstd` (o writer/encoder fica só na feature `c-zstd`),
> e um crate `flight-wasm` (cdylib) expõe um **ABI C cru** (`alloc`/`parse`/`dealloc`, retornando JSON com
> prefixo de tamanho) — sem wasm-bindgen, sem bundler. `scripts/build-wasm.sh` embute o `.wasm` como base64
> num único `viewer-wasm/index.html` **offline** (arrasta o `.flight` do `file://`, nada sobe, nada instala).
> **Middleware WSGI/ASGI** (`_web.py`): `FlightWSGI`/`FlightASGI` gravam um `.flight` por 500 com o contexto
> de trace da request (passado por-request, thread-safe), **agnóstico de framework** (fala os protocolos, não
> a API de nenhum). **`flight ci`** (`_ci.py`) + **GitHub Action** (`.github/actions/flight`): renderiza um
> comentário Markdown de causa-raiz (reusa `explain` + `fingerprint`) p/ o job summary / comentário de PR.
> **Recorders cross-language** (`recorders/go`, `recorders/node`): escrevem o **mesmo** formato `.flight` —
> msgpack à mão + um **frame zstd "stored"** (blocos raw, zstd válido sem compressor) → **zero dependências**,
> lidos de volta pelo reader Rust/Python. **Escopo honesto:** o round-trip AEAD e os testes Go/Node/WASM
> pulam limpo quando o toolchain (cryptography/go/node/wasm) falta. Testes: `tests/test_ecosystem.py` (plugin/
> crypto/middleware/ci) + `tests/test_polyglot.py` (Go/Node/WASM).

**Fase 10 — Moonshot: What-if debugging.** Já que reconstruímos estado em qualquer `seq`, permitir **editar
um valor no passado e re-executar dali para a frente** sobre a fita determinística: "e se `numbers` não
estivesse vazio aqui?" — o programa segue e você vê o resultado **contrafactual**. É o santo graal do
time-travel, e a arquitetura (event sourcing + tape) é uma das poucas que o torna factível.

> **Entregue (Fase 10, o moonshot).** `flight.what_if(path, fn, overrides)` (`_whatif.py`): **dois replays
> fiéis** do mesmo `fn` sobre a mesma fita — o **baseline** reproduz o resultado gravado bit-a-bit, e o
> **contrafactual** roda com um hook de trace (`sys.settrace`) que, no momento em que a execução chega a uma
> linha escolhida, **sobrescreve um local** com o seu valor. Mexer num local vivo sem cirurgia de bytecode é
> possível no Python **3.13+**, onde `frame.f_locals` é um proxy write-through (**PEP 667**): atribuir a ele
> num callback de trace atualiza o fast-local que o próximo bytecode lê (em Pythons mais velhos o override não
> pega e o `what_if` **diz isso** em vez de mentir). Três desfechos honestos caem do contrafactual: **retorna/
> levanta** algo diferente (o resultado contrafactual); **diverge** da fita (a mudança tomaria outro caminho
> pelo mundo gravado — ex.: chama `random()` uma vez a mais — o que é em si um achado: a edição é
> inconsistente com a gravação); ou **não alcança** o ponto de override (reportado, não ignorado em silêncio).
> Tudo respeita P1 — a exceção do *próprio* contrafactual é capturada e reportada (`Outcome`/`WhatIf.render`),
> nunca levantada em você. O `random`/tempo/IO ficam **constantes** (fita da Fase 3), então o contrafactual é
> reprodutível entre execuções. **Escopo honesto:** override por `(qualname, linha, nth)`, aplicado logo antes
> da linha (mira a linha que *usa* o valor, não a que atribui); requer 3.13+; é API (como `flight.minimize`).
> Testes: `tests/test_whatif.py` (8: conserta-o-crash, mudança inerte, override não-alcançado, divergência,
> determinístico).

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
