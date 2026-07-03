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

## Por quê

Um traceback diz **onde** um programa morreu, quase nunca **por quê**. O flight grava o que realmente
aconteceu, para que o bug report se escreva sozinho. As três apostas do projeto (ver [VISION.md](VISION.md)):
`sys.monitoring` finalmente torna barato instrumentar o CPython; ferramenta de debug é 50% engine e 50%
experiência de leitura; e o arquivo `.flight` compartilhável é o vetor viral.

## O que é (e o que não é)

**É** um gravador post-mortem de escopo delimitado, com um viewer de primeira classe, evoluindo para
time-travel debugging. **Não é** um APM, um debugger ao vivo (isso é o `pdb`) nem um profiler.

## Status — Fase 3 (re-execução) ✅

Todas as fases concluídas, ponta a ponta e testadas: 0 (fundação), 1 (a caixa-preta), 1.5 (viewer TUI),
2 (time-travel de escopo) e 3 (degraus 1–2 da re-execução).

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
Degrau 3 (threads) é pesquisa: replay garantido só single-thread/asyncio; arquivos/sockets estagiados.

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
- **Fase 8 — Caixa-preta de produção.** Governador adaptativo de overhead (SLO), daemon always-on + flush no
  crash (sobrevive a SIGKILL/OOM), correlação distribuída (OpenTelemetry).
- **Fase 9 — Laço viral e ecossistema.** Viewer no browser (reader Rust → WASM), plugin pytest, GitHub
  Action, middleware Django/FastAPI/Flask, recorders cross-language, cripto em repouso.
- **Fase 10 — Moonshot: what-if debugging.** Editar um valor no passado e re-executar dali sobre a fita
  determinística — o resultado contrafactual.

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
```

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
