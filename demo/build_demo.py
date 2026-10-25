"""Build the README clip's page from the event log of a real SIPp run, then record it with Playwright.

    python scripts/sipp_demo.py --out docs/sipp_run.json        # the real run
    python demo/build_demo.py docs/sipp_run.json scratch/demo   # the page, ready to record
    node demo/record.cjs scratch/demo docs/demo.mp4             # the recording

The page replays what the run logged: the SIP messages the line saw, the transcript with the time of each line, the keypad
digits, and the saved record. It plays at 4x and says so on screen. Nothing on the page is typed in by hand except captions.
"""
import json
import sys
from pathlib import Path

SPEED = 4.0


def events(run: dict) -> list[dict]:
    log = run["server_sip_log"]
    t_invite = next(t for d, line, t in log if d == "in" and line.startswith("INVITE"))
    t_ok = next(t for d, line, t in log if d == "out" and line.startswith("SIP/2.0 200"))
    ev = []

    def at(t, **kw):
        ev.append({"t": (t - t_invite) / SPEED, **kw})

    answered = False
    for d, line, t in log:
        if d == "in" and line.startswith("OPTIONS"):
            continue  # SIPp's keepalive (it holds Docker's UDP mapping open); the 200 that answers it is skipped below
        if d == "in" and line.startswith("INVITE"):
            at(t, pane="sip", cls="in", text="SIPp  ->  INVITE  sip:+13055550102  (offers PCMA, PCMU, telephone-event)")
        elif d == "out" and line.startswith("SIP/2.0 100"):
            at(t, pane="sip", cls="out", text="line  ->  100 Trying")
        elif d == "out" and line.startswith("SIP/2.0 180"):
            at(t, pane="sip", cls="out", text="line  ->  180 Ringing")
        elif d == "out" and line.startswith("SIP/2.0 200") and not answered:
            answered = True
            at(t, pane="sip", cls="out", text=f"line  ->  200 OK  answers {run['codec']}  (its own preference)")
            at(t, pane="cap", text=f"The line routed the dialled number to a firm and negotiated {run['codec']}, 8 kHz")
        elif d == "in" and line.startswith("ACK"):
            at(t, pane="sip", cls="in", text="SIPp  ->  ACK, RTP starts")
        elif d == "in" and line.startswith("BYE"):
            at(t, pane="sip", cls="in", text="SIPp  <-  BYE from the line, call over")
        elif d == "out" and line.startswith("BYE"):
            at(t, pane="sip", cls="out", text="line  ->  BYE, intake finished")
    digits = "7 8 6 5 5 5 0 1 4 7 #"
    for item in run["transcript"]:
        t = t_ok + item["t"]
        if item["speaker"] == "AGENT" and "seven eight six" in item["text"]:
            at(t - 0.2, pane="sip", cls="in", text=f"SIPp  ->  RFC 4733 digits  {digits}")
            at(t - 0.2, pane="cap", text="Keypad digits arrive as RFC 4733 events and fill the phone field")
        at(t, pane="say", who=item["speaker"], text=item["text"])
        if item["speaker"] == "AGENT" and item["text"].startswith("I have Dana"):
            at(t, pane="cap", text="Every field is read back and waits for a yes before it is stored")
    first_caller = next((t_ok + i["t"] for i in run["transcript"] if i["speaker"] == "CALLER"), t_ok + 10)
    for frac, text in ((0.35, "The agent greets, says it is automated and gives the recording notice"),
                       (0.7, "It then asks for a name. SIPp answers with audio from a recorded call")):
        at(t_ok + (first_caller - t_ok) * frac, pane="cap", text=text)
    last = max(e["t"] for e in ev)
    ev.append({"t": last + 0.8, "pane": "record"})
    ev.append({"t": last + 0.8, "pane": "cap", "text": "The record carries the tenant's config version and hash, and is queued to two receivers"})
    ev.append({"t": last + 2.0, "pane": "end"})
    return sorted(ev, key=lambda e: e["t"])


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>sipclerk replay</title>
<style>
:root{--bg:#0f1318;--panel:#171d25;--ink:#e6edf3;--mute:#8b98a5;--agent:#7ee0b0;--caller:#8fc2ff;--line:#2a3441;--warn:#ffd479}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.35 Consolas,"Cascadia Mono",monospace;width:1280px;height:720px;overflow:hidden}
header{height:46px;display:flex;align-items:center;justify-content:space-between;padding:0 18px;border-bottom:1px solid var(--line);font-size:18px}
header b{color:var(--agent)}header span{color:var(--mute)}
main{display:grid;grid-template-columns:380px 1fr 380px;gap:12px;padding:12px;height:612px}
section{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px;overflow:hidden}
h2{margin:0 0 8px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);font-weight:600}
.row{margin:0 0 6px;white-space:pre-wrap}.in{color:var(--caller)}.out{color:var(--agent)}
.say{margin:0 0 7px;padding:5px 8px;border-radius:5px;background:#1d2631}.say b{display:block;font-size:12px;letter-spacing:.06em;color:var(--mute)}
.say.AGENT{border-left:3px solid var(--agent)}.say.CALLER{border-left:3px solid var(--caller)}
table{width:100%;border-collapse:collapse;font-size:16px}td{padding:3px 0;border-bottom:1px solid var(--line);vertical-align:top}td:first-child{color:var(--mute);width:42%}
.tag{color:var(--warn)}
footer{position:absolute;left:0;right:0;bottom:0;height:62px;background:#0a0d11;border-top:1px solid var(--line);display:flex;align-items:center;padding:0 22px;font:600 25px/1.2 system-ui,Segoe UI,sans-serif}
#log,#talk{height:520px;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-end}
</style></head><body>
<header><div><b>sipclerk</b> inbound intake line, a real SIPp call</div><span>replay of the logged run at 4x, invented firm and caller</span></header>
<main>
<section><h2>SIP and RTP seen by the line</h2><div id="log"></div></section>
<section><h2>The call</h2><div id="talk"></div></section>
<section><h2>Saved record</h2><div id="rec"><p class="row" style="color:var(--mute)">written when the call ends</p></div></section>
</main>
<footer id="cap">A separate SIP client places a real INVITE at the line</footer>
<script>
const EVENTS=__EVENTS__, RECORD=__RECORD__;
const $=id=>document.getElementById(id);
function add(box,html,cls){const d=document.createElement('div');d.className=cls;d.innerHTML=html;$(box).appendChild(d);}
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function record(){
  const r=RECORD, rows=[['tenant',r.tenant],['config','v'+r.version+' '+r.hash],['codec',r.codec],['outcome',r.outcome],...Object.entries(r.fields).map(([k,v])=>[k,v])];
  $('rec').innerHTML='<table>'+rows.map(([k,v])=>'<tr><td>'+esc(k)+'</td><td>'+esc(v)+'</td></tr>').join('')+'</table>'
   +'<p class="row tag">queued: case-management receiver, call console</p>';
}
const t0=performance.now();
EVENTS.forEach(e=>setTimeout(()=>{
  if(e.pane==='sip')add('log',esc(e.text),'row '+e.cls);
  else if(e.pane==='say')add('talk','<b>'+e.who+'</b>'+esc(e.text),'say '+e.who);
  else if(e.pane==='cap')$('cap').textContent=e.text;
  else if(e.pane==='record')record();
  else if(e.pane==='end')window.__done=true;
},e.t*1000));
</script></body></html>
"""


def main() -> int:
    run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    f = run["fields"]
    record = {"tenant": run["tenant_id"], "version": run["config_version"], "hash": run["config_hash"][:12], "codec": run["codec"], "outcome": run["outcome"],
              "fields": {k: ("kept private" if k == "callback_number" and False else v) for k, v in f.items() if v is not None}}
    if run.get("field_sources", {}).get("callback_number"):
        record["fields"]["callback_number"] = f"{f['callback_number']} (from {run['field_sources']['callback_number']})"
    html = PAGE.replace("__EVENTS__", json.dumps(events(run))).replace("__RECORD__", json.dumps(record))
    (out / "index.html").write_text(html, encoding="utf-8")
    print(f"{len(events(run))} events, {max(e['t'] for e in events(run)):.1f} s at {SPEED}x -> {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
