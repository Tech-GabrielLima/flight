// Records a tiny run and writes a `.flight` — the same black-box format the
// Python/Rust engine reads. Run as:
//
//   node demo.mjs /path/to/out.flight
//
// Then, from Python:  flight.read("/path/to/out.flight")  — or  flight inspect.
import process from "node:process";
import { Recorder, KindStart, KindLine, KindRaise, KindUnwind } from "./flight.mjs";

const out = process.argv[2];
if (!out) {
  console.error("usage: node demo.mjs <out.flight>");
  process.exit(2);
}

const rec = new Recorder();
rec.registerCode(1, "app.js", "main", 1);
rec.registerCode(2, "app.js", "handleRequest", 20);

rec.record(KindStart, 0, 0, 1); // main() started
rec.record(KindStart, 0, 20, 2); // handleRequest() started
rec.record(KindLine, 0, 25, 2); // a line ran
rec.record(KindRaise, 0, 25, 2); // an error was raised
rec.record(KindUnwind, 0, 20, 2);
rec.record(KindUnwind, 0, 1, 1);

rec.dump(out);
console.log(`wrote ${out} (${rec.length} events)`);
