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

## Status — Fase 0 (fundação) ✅

Esta entrega é o *hello-world de toda a stack*, ponta a ponta e totalmente testado:

- **`flight-format`** — o formato `.flight` versionado, append-only e tolerante a truncamento.
- **`flight-reader`** — parser tolerante: usa o índice do footer quando existe, cai para scan linear
  quando não; mantém blocos desconhecidos como bytes crus; degrada para `partial` em vez de falhar.
- **`flight-core`** — o caminho quente em Rust: ring buffer lock-free por thread, relógio lógico
  global, mapa de códigos e o writer, exposto ao Python como `flight._core`.
- **`flight` (Python)** — `install()`/`uninstall()` ligando o `sys.monitoring`, um `excepthook` que
  gera o `.flight` no crash, `capture()` para erros tratados, e a CLI `python -m flight run|inspect`.

Um `.flight` da Fase 0 contém o **ambiente** (META) e o **ring de eventos** — os últimos milhares de
eventos PY_START / LINE / RETURN / RAISE de todas as threads, mesclados por tempo lógico. Isso já
responde *"que caminho o código percorreu nos instantes antes de morrer?"*.

**Próximo:** a Fase 1 adiciona frames, locals e o grafo de objetos serializado; a Fase 1.5, um viewer TUI.

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
