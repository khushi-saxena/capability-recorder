from src.surface.playwright_surface import PlaywrightSurface
from src.surface.base import resolve, bind_templates
from src.schema import Capability

cap = Capability.model_validate_json(open("artifacts/member_savings_lookup.v1.json").read())
params = {"member_id": "12345"}
s = PlaywrightSurface(headless=False)

s.navigate("http://127.0.0.1:8800/")
obs = s.observe()
tb = [n for n in obs.nodes if n.role == "textbox"]
s.fill(tb[0].handle, "op1")
s.fill(tb[1].handle, "x")
s.activate(next(n.handle for n in obs.nodes if n.role == "button"))

for step in cap.flow:
    if step.intent == "navigate":
        s.navigate(step.url_template)
        obs = s.observe()
        continue
    obs = s.observe()
    r = resolve(bind_templates(step.target, params), obs)
    print(step.id, "->", r.strategy, "tier", r.tier)
    if step.intent == "fill":
        s.fill(r.handle, step.value_template.replace("{{member_id}}", params["member_id"]))
    elif step.intent == "activate":
        s.activate(r.handle)
    elif step.intent == "read":
        print("   ", step.output_key, "=", repr(s.read(r.handle)))

s.close()