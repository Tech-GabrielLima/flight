#!/usr/bin/env bash
# Build the .flight reader to WebAssembly and produce the self-contained,
# offline browser viewer at viewer-wasm/index.html (the wasm is inlined as
# base64, so the single HTML file works straight from file:// — no server).
#
# Needs: the wasm32 target (`rustup target add wasm32-unknown-unknown`).
set -euo pipefail
cd "$(dirname "$0")/.."

WASM_CRATE="crates/flight-wasm"
OUT_WASM="$WASM_CRATE/target/wasm32-unknown-unknown/release/flight_wasm.wasm"

echo "building flight-wasm (release, wasm32)…"
( cd "$WASM_CRATE" && cargo build --release --target wasm32-unknown-unknown )

# wasm-opt is optional; use it to shrink if present.
if command -v wasm-opt >/dev/null 2>&1; then
  echo "optimizing with wasm-opt…"
  wasm-opt -Oz "$OUT_WASM" -o "$OUT_WASM.opt" && mv "$OUT_WASM.opt" "$OUT_WASM"
fi

echo "inlining into viewer-wasm/index.html…"
python3 - "$OUT_WASM" viewer-wasm/template.html viewer-wasm/index.html <<'PY'
import base64, sys
wasm, template, out = sys.argv[1], sys.argv[2], sys.argv[3]
b64 = base64.b64encode(open(wasm, "rb").read()).decode("ascii")
html = open(template).read().replace("__WASM_BASE64__", b64)
open(out, "w").write(html)
print(f"  wrote {out}  ({len(html)//1024} KiB, wasm {len(b64)*3//4//1024} KiB)")
PY

# Ship the same page inside the package, so `flight view --serve` can serve it
# from an installed wheel (it injects the recording + a live /whatif and /fix).
cp viewer-wasm/index.html python/flight/_viewer.html
echo "  copied to python/flight/_viewer.html (for flight view --serve)"
echo "done — open viewer-wasm/index.html in any browser and drop a .flight."
