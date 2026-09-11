from src.surface.playwright_surface import PlaywrightSurface
from src.surface.base import resolve, bind_templates
from src.schema import Capability

cap = Capability.model_validate_json(open("artifacts/member_savings_lookup.v1.json").read())
s = PlaywrightSurface(headless=False)

s.navigate("http://127.0.0.1:8800/")
obs = s.observe()
tb = [n for n in obs.nodes if n.role == "textbox"]
s.fill(tb[0].handle, "op1")
s.fill(tb[1].handle, "x")
s.activate(next(n.handle for n in obs.nodes if n.role == "button"))

s.navigate("http://127.0.0.1:8800/console")
obs = s.observe()
print("frames:", sorted({n.frame_path for n in obs.nodes}))
for n in obs.nodes:
	if n.role in ("textbox", "button", "link"):
		print(" ", n.handle, n.frame_path, n.role, repr(n.name))

for sid in ("enter_member_id", "submit_search"):
	step = next(x for x in cap.flow if x.id == sid)
	try:
		print(sid, "->", resolve(bind_templates(step.target, {"member_id": "12345"}), obs))
	except Exception as e:
		print(sid, "FAILED:", e)

s.close()
