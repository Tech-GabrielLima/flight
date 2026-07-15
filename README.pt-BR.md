# flight — um gravador de voo para Python

> **Idiomas:** [English](README.md) · **Português**
>
> **Docs:** [Visão & produto](VISION.md) · [Guia técnico](TECHNICAL.md) · [O formato `.flight`](docs/FORMAT.md)

> Quando um programa Python morre, você não deveria receber um traceback e *boa sorte* — deveria
> receber a **caixa-preta** completa do voo: cada passo que o código deu nos seus últimos instantes,
> navegável, compartilhável e (no futuro) reexecutável no tempo.
>
> flight é um **gravador post-mortem** feito para você poder deixar ligado: um ring buffer lock-free e
> o writer do arquivo `.flight` vivem em **Rust** (via PyO3), alimentados pelo **`sys.monitoring`** do
> CPython (PEP 669, 3.12+) para manter o overhead em regime baixo. Quando uma exceção não tratada
> escapa, a gravação é despejada num arquivo `.flight` autodescritivo e tolerante a truncamento.

```console
$ python -m flight run examples/crash.py
Traceback (most recent call last):
  ...
ZeroDivisionError: division by zero
[flight] recorded flight-48103-1783001126460.flight

$ python -m flight inspect flight-48103-1783001126460.flight
flight file : flight-48103-1783001126460.flight
format      : v1  (complete, index)
blocks      : META, EVENT_RING
events      : 16 across 4 code objects
last events (most recent first):
    PY_UNWIND  crash.py:0      # ZeroDivisionError subindo a pilha…
    RAISE      crash.py:0      # …frame a frame: compute_average → summarize → main
    PY_START   crash.py:4      # compute_average tinha acabado de ser chamada
    ...
```

## Como funciona

Um caminho de entrada, um arquivo de saída, várias formas de ler.

```text
   seu programa
        │   sys.monitoring (PEP 669) — callbacks nativos em Rust, sem frame Python, sem salto de FFI
        ▼
┌──────────────────────── no processo · caminho quente · Rust ─────────────────┐
│  "esse código é meu?"          ring buffer lock-free             relógio       │
│   cache thread-local   ──▶      por thread (push de       ──▶     lógico global │
│   (sem lock, ~25 ns)            24 bytes, sem mutex)              (junta threads)│
└───────────────────────────────────────┬──────────────────────────────────────┘
                                         │  exceção não tratada  ·  ou capture()
                                         ▼
     grafo de objetos (preserva identidade, aliasing ↔)  +  locals de cada frame
           +  a cadeia da exceção  +  o código-fonte de cada arquivo envolvido
                                         │
                                         ▼
                              ┌───────────────────────┐
                              │      crash.flight      │   msgpack + zstd · versionado
                              │   uma caixa-preta que  │   tolerante a truncamento
                              │      se descreve       │   compartilhável
                              └───────────┬───────────┘
             ┌───────────────────────────┼───────────────────────────────┐
             ▼                            ▼                               ▼
      flight inspect              viewer no navegador (WASM)       why · diff · fix
      CLI · TUI Textual           abre offline, nada é             bisect · generalize
                                  enviado, sem instalar            what-if · serve
```

**Grave barato o bastante pra deixar ligado.** O `sys.monitoring` chama direto em **Rust nativo** — sem
frame de callback Python, sem segundo salto de FFI. O caminho quente não pega lock: um cache thread-local
responde "esse código é meu?", e os eventos vão para um **ring buffer lock-free por thread**. **No crash,
escreva uma caixa-preta — não um trace:** o `excepthook` serializa o **grafo de objetos** por identidade
(o *mesmo* objeto em dois frames vira um só, marcado `↔`), os locals de cada frame, a cadeia da exceção e o
**código-fonte** — tudo com orçamento de profundidade/bytes/tempo, e sem nunca poder derrubar o programa
gravado. **Leia em qualquer lugar:** o `.flight` é a espinha — CLI, TUI, o **viewer WASM** offline e toda
análise falam só com o arquivo, nunca com um processo vivo.

### Abra no navegador — sem instalar nada

O reader em Rust é compilado para **WebAssembly** dentro de uma única página HTML autossuficiente. Arraste
um `.flight` e o crash é interpretado **no seu navegador**, offline — nada é enviado. É o artefato
compartilhável que sustenta o projeto inteiro: *"abra isto e você vai ver tudo."*

<div align="center">
<img alt="o viewer WASM no navegador: um .flight interpretado offline, mostrando a exceção, os frames e o grafo de objetos"
     width="820"
     src="https://raw.githubusercontent.com/Tech-GabrielLima/flight/main/assets/viewer.png">
</div>

## Por quê

Um traceback diz **onde** um programa morreu, quase nunca **por quê**. O flight grava o que realmente
aconteceu, para que o bug report se escreva sozinho. As três apostas do projeto (ver [VISION.md](VISION.md)):
`sys.monitoring` finalmente torna barato instrumentar o CPython; ferramenta de debug é 50% engine e 50%
experiência de leitura; e o arquivo `.flight` compartilhável é o vetor viral.

## O que é (e o que não é)

**É** um gravador post-mortem de escopo delimitado, com um viewer de primeira classe, evoluindo para
time-travel debugging. **Não é** um APM, um debugger ao vivo (isso é o `pdb`) nem um profiler.

## Status — todas as fases (0–10) concluídas ✅

Todo o roadmap está pronto, ponta a ponta e testado: 0 (fundação), 1 (a caixa-preta), 1.5 (viewer TUI),
2 (time-travel de escopo), 3 (re-execução), 4 (I/O determinístico + escalonamento), 5 (depurador reverso +
DAP), 6 (diff + delta-debug), 7 (inteligência), 8 (caixa-preta de produção), 9 (ecossistema) e 10 (moonshot:
what-if debugging).

- **`flight-format`** — o formato `.flight` versionado, append-only e tolerante a truncamento.
- **`flight-reader`** — parser tolerante (índice do footer + scan linear; blocos desconhecidos como
  bytes crus; degrada para `partial`) com API de consulta: exceção, frames, grafo de objetos e **aliasing**.
- **`flight-core`** — o caminho quente em Rust: ring buffer lock-free, relógio lógico, writer, `flight._core`.
- **`flight` (Python)** — `install()`/`uninstall()`, `excepthook` que grava a caixa-preta do crash,
  `capture()`, e a CLI `python -m flight run|inspect`.

Um `.flight` da Fase 1 contém, além do **ambiente** (META) e do **ring de eventos**: a **cadeia de
exceções**, todos os **frames** (do crash para fora) com seus **locais**, o **grafo de objetos**
serializado (preserva identidade/aliasing, seguro contra ciclos, com limites e orçamento de tempo/bytes)
e a **fonte** de cada arquivo. Duas propriedades de primeira classe: **scrubbing** (P5) redige valores
sensíveis por nome antes de gravar, e o gravador **nunca derruba** o programa (P1). **Adaptadores**
descrevem objetos grandes (numpy/pandas) por forma/dtype/preview.

**Fase 2 — time-travel de escopo.** Dentro de `with flight.record():`, cada escrita de estado vira um
MUTATION, e depois você reexecuta a memória do programa: `flight timeline --var x` (evolução de uma
variável), `--who cache` ("quem mutou este dict"), e `Recording.state_at(seq)` reconstrói os locais num
instante (event sourcing). Captura robusta e à prova de versão, sem cirurgia de bytecode: por evento
LINE no escopo, diff dos locais (rebinds) e diff de snapshot de objetos sob `watch()` (escritas em
contêiner/atributo, sem quebrar `type()`). Opt-in e delimitado (P2).

**Fase 1.5 — o viewer.** Um TUI ([Textual](https://textual.textualize.io)) sobre a API do reader:
`pip install 'flight-recorder[viewer]'` e `python -m flight view arquivo.flight`. À esquerda, uma
árvore **frames → locais → grafo de objetos** com expansão lazy (aliasing marcado com `↔`); à direita,
abas com o **código** do frame (linha do crash marcada e **valores inline**), **Detail** do objeto,
**Exception**, **Events** (o ring) e **Timeline** (mutações da Fase 2). A lógica de render fica em
`_viewer_model` (testada sem terminal); o app é testado *headless* via o `Pilot` do Textual.

**Fase 3 — re-execução.** *Degrau 1:* `flight repro crash.flight` gera um `repro_bug.py` autocontido e
**verificado** — reconstrói os argumentos do frame do crash a partir do grafo de objetos (aliasing/ciclos
preservados; objetos opacos viram stubs), embute a fonte, chama a função e confere a exceção em subprocesso.
*Degrau 2:* `with flight.deterministic(path):` grava a não-determinação (time/random/uuid/urandom/secrets/…)
num bloco NONDET, e `flight.replay(path, fn)` reexecuta bit a bit; `ReplayDivergence` aponta onde o fluxo
divergiu. *Convergência:* um crash dentro de `deterministic()` grava frames **e** a fita no mesmo arquivo,
e o `repro` tece a fita no script — **reproduzindo um crash flaky de tempo/aleatoriedade deterministicamente**.
**Fase 4 — fidelidade total de replay (concluída).** `flight.deterministic()` também grava **o que o código
leu** — arquivos (texto/binário, `read`/`readline`/`readinto`/iteração), pipes (`os.read`), saída de
subprocessos (`subprocess.run`/`check_output`) e sockets (`recv`/`recv_into`) — na mesma fita. O replay é
**offline** (leituras vêm da fita; escritas são engolidas). Leituras acima de `io_hash_above` (256 KiB por
padrão) viram *comprimento + digest BLAKE2b* e são **verificadas** contra a fonte viva no replay;
`io_hash_above=0` inlina tudo. Para asyncio, a **ordem de conclusão das tasks** é gravada e verificada. Para
**threads**, a **ordem de aquisição de locks** é gravada e **imposta** no replay (threads numeradas por
ordem de início, cada uma com sua trilha da fita) — reproduz o bug flaky de "qual thread ganhou" bit a bit.
Honestidade: só locks do usuário criados no escopo; aquisições não-bloqueantes/com timeout não são
ordenadas; corridas sobre estado não-travado e multiprocessing ficam fora.

**Fase 5 — depurador reverso (concluída).** Sobre a timeline de MUTATION da Fase 2, um motor de time-travel
(`_timetravel.py`) dá um cursor que anda **para trás** e para frente; `state()` reconstrói locais e o
conteúdo de contêineres no cursor; e o **"breakpoint no passado"** — `find_first("running > 100")` — pula
para a escrita que primeiro satisfez a condição (parser seguro, sem `eval`). Exposto via **DAP**
(`_dap.py`) anunciando `supportsStepBack` ⇒ **VS Code/PyCharm mostram Step Back/Reverse de graça**. CLI:
`flight debug arquivo.flight` (servidor DAP) ou `--find "running > 100"` / `--list`. Granularidade por-linha
(sub-linha depende do bytecode nativo, fase futura).

**Fase 6 — debugging por comparação (concluída).** `flight diff a.flight b.flight` (`_diff.py`) alinha duas
gravações **posição a posição** e aponta a primeira divergência, no eixo mais rico que ambas compartilham:
timeline de MUTATION (primeira escrita cujo alvo/valor difere), fita NONDET (primeira chamada de fronteira
que respondeu diferente; mismatch de *source* = fluxo ramificou — a raiz de um teste flaky) ou o ring de
eventos. Sai com código 1 quando divergem (como o `diff(1)`). **Delta debugging** (`_ddmin.py`): o `ddmin`
de Zeller ligado ao replay — `flight.minimize(path, fn)` substitui valores gravados por um default neutro e
mantém só as reduções que ainda falham, até sobrar o conjunto mínimo *load-bearing*: "seu bug precisa só
destes N valores". `ddmin` genérico é função pura testada à parte.

**Fase 7 — camada de inteligência (concluída).** `flight explain` (`_explain.py`): um **resumo heurístico
de causa-raiz offline** (exceção + frame do crash + locais suspeitos — vazio/None/zero — com palpite
dirigido: ZeroDivisionError→divisor zero, etc.) **e** um **prompt pronto para LLM** empacotando a cadeia de
exceção + stack + código + locais; o provider de modelo é injetável (`provider(prompt)→texto`), opt-in via
`--llm` (Anthropic), e falha de modelo nunca quebra o explain (P1). `flight repro --pytest`: emite um
**teste de regressão commitável** (`pytest.raises`) que também se auto-verifica como script. **Query
semântica** de tamanho — `len(cache) > 100` — no motor de time-travel ("quando `cache` passou de 100?").
`flight fingerprint` (`_fingerprint.py`): hash estável por cadeia de exceções + `(qualname, arquivo,
offset)` de cada frame + *kinds* dos locais — dedup estilo Sentry, mas por **frame + estado**.

**Fase 8 — caixa-preta de produção (concluída).** Tudo Python puro sobre o motor existente (correlação
viaja na fita NONDET; granularidade retunada ao vivo via `sys.monitoring`). **Governador de overhead como
SLO** (`_governor.py`): um `OverheadLadder` puro com histerese escolhe uma granularidade (`lines → returns →
calls`) e um `Governor` amostra a vazão de eventos num thread de fundo, estima a fração de wall-clock gasta
gravando e **desce um degrau** quando o código gravado vira loop quente — largando LINE, depois RETURN,
nunca abaixo de "quais funções rodaram e como desenrolou" — **subindo** de volta quando esfria.
`flight.install(overhead_slo=0.03)` / `flight run --slo 0.03`. **Supervisor que sobrevive à morte do avião**
(`_daemon.py`): um thread grava *checkpoints* atômicos do ring e um processo supervisor compartilha um pipe
com o pai — desligamento limpo manda um byte e o checkpoint é descartado; numa **morte incatável**
(`SIGKILL`/OOM/segfault, nenhum Python roda) o pipe fecha, o supervisor recebe EOF e **promove o último
checkpoint** a `flight-killed-*.flight`. `flight.start_daemon()` / `flight run --daemon`. **Correlação
distribuída** (`_correlation.py`): `TraceContext` do W3C Trace Context, lido de header explícito, de um span
**OpenTelemetry** ao vivo (opcional) ou do ambiente; `flight.correlate(...)` carimba o contexto em todo
black box, `flight.link(...)` referencia serviços upstream, e `flight trace <dir>` agrupa os `.flight` por
`trace_id` → o **grafo de crash cross-service**. Escopo honesto: o overhead é estimado por custo-por-evento
calibrado (single-thread; superestima em muitos cores, seguro para um SLO); o checkpoint é periódico (um
kill duro pode perder até um intervalo dos últimos eventos); é supervisor-sobre-checkpoint, não ainda um
ring em memória compartilhada lido ao vivo (refino futuro).

**Fase 9 — ecossistema (concluída).** **Plugin pytest** (`_pytest.py`, entry point `pytest11`): `pytest
--flight` grava um `.flight` por teste que falha (nunca muda o resultado, P1). **Cripto em repouso**
(`_crypto.py`): `flight encrypt`/`decrypt` — envelope `FLGTENC1|salt|nonce|ct+tag`, KDF **scrypt** (stdlib),
AEAD **AES-256-GCM** (`cryptography`, extra `[crypto]`; sem o pacote → `CryptoUnavailable` claro). **Viewer
no browser** (`crates/flight-wasm` + `viewer-wasm/`): o `flight-reader` compilado p/ **WASM** — como o `zstd`
é dep C, o decode passa a **`ruzstd`** (Rust puro) na feature `pure-zstd`, e um ABI C cru (`alloc`/`parse`/
`dealloc`) dispensa wasm-bindgen; `scripts/build-wasm.sh` embute o `.wasm` em base64 num `index.html`
**offline** (arrasta o `.flight` do `file://`). **Middleware** (`_web.py`): `FlightWSGI`/`FlightASGI` gravam
um `.flight` por 500 com o trace da request, **agnóstico de framework**. **`flight ci`** (`_ci.py`) +
**GitHub Action** (`.github/actions/flight`): comentário Markdown de causa-raiz p/ CI vermelho. **Recorders
cross-language** (`recorders/go`, `recorders/node`): escrevem o **mesmo** `.flight` — msgpack à mão + frame
zstd "stored" (blocos raw) → **zero deps**, lidos de volta pelo reader Rust/Python.

**Fase 10 — moonshot: what-if debugging (concluída).** `flight.what_if(path, fn, overrides)` (`_whatif.py`):
dois replays fiéis sobre a mesma fita — **baseline** reproduz o resultado gravado bit-a-bit, **contrafactual**
roda com um hook `sys.settrace` que, ao chegar numa linha escolhida, **sobrescreve um local** (via proxy
write-through do `frame.f_locals`, **PEP 667**, Python 3.13+). Desfechos: retorna/levanta algo diferente, ou
**diverge** da fita (a mudança tomaria outro caminho pelo mundo gravado — um achado em si), ou **não alcança**
o ponto (reportado). Tempo/random/IO ficam constantes (fita da Fase 3) → contrafactual reprodutível. É API
(como `minimize`), requer 3.13+.

## Roadmap adiante — Fases 4–10

A bússola: **fidelidade → experiência → inteligência → alcance**. Toda fase mantém os cinco invioláveis
(P1–P5) e é útil sozinha (P4). Contratos completos em [VISION.md](VISION.md) §5.6.

- **Fase 4 — Fidelidade total de replay (fechar o degrau 3).** Interpor arquivos/sockets/subprocess **e o
  escalonamento**: gravar a *ordem* de aquisição de locks e retomada de tasks (threads e asyncio), não os
  dados internos. Grave a ordem em que o mundo respondeu e o replay multi-thread repete bit a bit.
  Entregável: `flight.deterministic()` cobrindo o **crash flaky de concorrência**. Parte difícil honesta:
  I/O grande infla o arquivo → modo "grava só o que foi lido, com hash do resto".
- **Fase 5 — Depurador reverso de verdade.** **Step-backward** + "breakpoint no passado" sobre
  `state_at(seq)`, com granularidade **sub-linha** via bytecode nativo (TECHNICAL §3.2), exposto por **DAP**
  → VS Code e PyCharm de graça.
- **Fase 6 — Debugging por comparação.** `flight diff a.flight b.flight` (primeira divergência) + **delta
  debugging** (ddmin sobre a fita → repro mínimo).
- **Fase 7 — Camada de inteligência.** `flight explain` (causa-raiz + patch por LLM), `flight repro --pytest`
  (teste de regressão commitável), query semântica na timeline, dedup por frame+estado.
- **Fase 8 — Caixa-preta de produção (concluída).** Governador adaptativo de overhead (SLO), daemon
  always-on + flush no crash (sobrevive a SIGKILL/OOM), correlação distribuída (W3C `traceparent` /
  OpenTelemetry) com `flight trace`.
- **Fase 9 — Laço viral e ecossistema (concluída).** Viewer no browser (reader Rust → WASM, offline),
  plugin `pytest --flight`, `flight ci` + GitHub Action, middleware WSGI/ASGI, recorders Go+Node no mesmo
  formato, cripto em repouso (AES-256-GCM).
- **Fase 10 — Moonshot: what-if debugging (concluída).** `flight.what_if` edita um valor no passado e
  re-executa dali sobre a fita determinística — o resultado contrafactual, com o mundo gravado constante.

## Instalar & compilar

Requer Python **3.12+** e um toolchain Rust.

```console
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest
maturin develop
```

## Uso

```python
import flight
flight.install()           # começa a gravar
# ... roda seu programa; numa exceção não tratada um .flight é escrito automaticamente
flight.capture()           # ou despeja um .flight agora (ex.: dentro de um except)
flight.stats()             # {'total_events': ..., 'threads': ..., 'codes': ..., 'ring_capacity': ...}
flight.uninstall()
```

Ou embrulhe um script sem editá-lo:

```console
python -m flight run meu_script.py --seus --args
python -m flight inspect crash.flight
python -m flight run --slo 0.03 --daemon servico.py   # overhead como SLO + sobrevive a kill -9 / OOM
python -m flight trace ./crashes                       # grafo de crash cross-service (por trace id)
python -m flight encrypt crash.flight --passphrase "$KEY"   # cripto em repouso (extra [crypto])
python -m flight ci .flight                            # comentário Markdown de causa-raiz p/ CI
pytest --flight                                        # um .flight por teste que falha (plugin)
```

Viewer no browser (offline, sem instalar nada): rode `scripts/build-wasm.sh` e abra `viewer-wasm/index.html`,
arrastando um `.flight`. Recorders cross-language em `recorders/go` e `recorders/node` escrevem o mesmo formato.

Em produção (Fase 8): `flight.install(overhead_slo=0.03, daemon=True)` liga o governador e o supervisor;
`flight.correlate(service="checkout")` carimba o contexto de trace (ambiente / OTel / explícito) e
`flight.link(caminho_flight_upstream)` referencia o black box do serviço anterior.

## Overhead — o retrato honesto

O flight grava só o *seu* código (stdlib e site-packages são excluídos por padrão) e, por padrão, na
granularidade de **chamada/retorno/exceção** — barato, e suficiente para responder "que funções rodaram
e como a exceção se propagou?". O detalhe por linha (`record_lines=True`) é opt-in. O custo por evento
(~350–500 ns) é dominado pelo callback Python do `sys.monitoring` e pelo salto de FFI — *não* pelo ring
em Rust. Atingir o alvo de <5% em código hot exige mover o callback para código nativo, uma otimização
nomeada da Fase 1. Rode `python scripts/bench.py` para o baseline. Detalhes no [README](README.md#overhead--the-honest-picture)
e em [TECHNICAL.md](TECHNICAL.md) §0.2.

## Testes

```console
cargo test                 # Rust: round-trips do formato, truncamento byte a byte, ring, recorder
pytest                     # Python: fiação do monitoring, captura no crash, CLI, round-trip
python scripts/bench.py    # baseline de overhead
```

## Licença

MIT.
