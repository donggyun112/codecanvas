"""Quick test for Phase 1: Static endpoint map."""
import json
import sys
sys.path.insert(0, "core")

from codecanvas.parser.fastapi_extractor import FastAPIExtractor
from codecanvas.graph.builder import FlowGraphBuilder

SAMPLE_PROJECT = "./sample-fastapi"
SAMPLE_SCRIPT_PROJECT = "./sample-script"

print("=" * 60)
print("Phase 1 Test: Static Endpoint Map")
print("=" * 60)

# 1. Extract endpoints
print("\n--- FastAPI Endpoint Extraction ---")
extractor = FastAPIExtractor(SAMPLE_PROJECT)
endpoints = extractor.analyze()
for ep in endpoints:
    print(f"  {ep.method:6s} {ep.path:30s} -> {ep.handler_name} ({ep.handler_file}:{ep.handler_line})")
    if ep.dependencies:
        print(f"         Depends: {ep.dependencies}")

print(f"\n  Total endpoints: {len(endpoints)}")
print(f"  Middlewares: {[m.class_name for m in extractor.middlewares]}")
print(f"  Exception handlers: {[h.exception_class for h in extractor.exception_handlers]}")

# 1b. Extract generic entry points
print("\n--- Generic Entry Point Extraction (sample-fastapi) ---")
builder = FlowGraphBuilder(SAMPLE_PROJECT)
entrypoints = builder.get_entrypoints()
for entry in entrypoints:
    print(
        f"  [{entry.kind:8s}] {entry.label:35s} -> "
        f"{entry.handler_name} ({entry.handler_file}:{entry.handler_line})"
    )
print(f"\n  Total entry points: {len(entrypoints)}")

# 2. Build flow for POST /login
print("\n--- Flow Graph for POST /api/v1/auth/login ---")
target = None
for ep in builder.get_endpoints():
    if "login" in ep.handler_name:
        target = ep
        break

if target:
    flow = builder.build_flow(target)
    print(f"  Nodes: {len(flow.nodes)}")
    for nid, node in flow.nodes.items():
        conf = node.confidence.value
        desc = f" - {node.description}" if node.description else ""
        print(f"    [{node.level}] {node.node_type.value:12s} {node.display_name:30s} ({conf}){desc}")
    print(f"\n  Edges: {len(flow.edges)}")
    for edge in flow.edges:
        label = f" [{edge.condition}]" if edge.condition else ""
        err = " (ERROR)" if edge.is_error_path else ""
        print(f"    {edge.source_id} -> {edge.target_id} ({edge.edge_type.value}){label}{err}")

    has_assignment = any(node.node_type.value == "assignment" for node in flow.nodes.values())
    has_branch = any(node.node_type.value == "branch" for node in flow.nodes.values())
    has_return = any(node.node_type.value == "return" for node in flow.nodes.values())
    print(f"\n  L4 assignments present: {has_assignment}")
    print(f"  L4 branches present: {has_branch}")
    print(f"  L4 returns present: {has_return}")
    if not has_assignment or not has_branch or not has_return:
        raise AssertionError("function logic layer is missing assignment/branch/return nodes")

    # Save JSON output
    with open("test_flow_output.json", "w") as f:
        json.dump(flow.to_dict(), f, indent=2)
    print(f"\n  Flow JSON saved to test_flow_output.json")
else:
    print("  ERROR: login endpoint not found!")
    print(f"  Available: {[(e.method, e.path) for e in builder.get_endpoints()]}")

# 3. Verify dependency bodies are merged into request flow
print("\n--- Dependency Flow for GET /users/me ---")
me_endpoint = next((ep for ep in builder.get_endpoints() if ep.handler_name == "get_me"), None)
if not me_endpoint:
    raise AssertionError("get_me endpoint not found")

me_flow = builder.build_flow(me_endpoint)
has_verify_token = any(node.name == "verify_token" for node in me_flow.nodes.values())
has_auth_401 = any(
    node.node_type.value == "exception" and node.metadata.get("status_code") == 401
    for node in me_flow.nodes.values()
)
print(f"  verify_token in flow: {has_verify_token}")
print(f"  dependency 401 path: {has_auth_401}")
if not has_verify_token or not has_auth_401:
    raise AssertionError("Dependency execution path was not merged into GET /users/me flow")

missing_desc = [node.id for node in me_flow.nodes.values() if not node.description]
print(f"  nodes missing descriptions: {len(missing_desc)}")
if missing_desc:
    raise AssertionError(f"Nodes still missing descriptions: {missing_desc}")

# 4. Verify API-free script entrypoints are discoverable
print("\n--- Script Entry Point Extraction (sample-script) ---")
script_builder = FlowGraphBuilder(SAMPLE_SCRIPT_PROJECT)
script_entrypoints = script_builder.get_entrypoints()
for entry in script_entrypoints:
    print(
        f"  [{entry.kind:8s}] {entry.label:35s} -> "
        f"{entry.handler_name} ({entry.handler_file}:{entry.handler_line})"
    )

script_target = next((entry for entry in script_entrypoints if entry.kind == "script"), None)
if not script_target:
    raise AssertionError("script entrypoint not found in sample-script")

script_flow = script_builder.build_flow(script_target)
print(f"  Script nodes: {len(script_flow.nodes)}")
print(f"  Script edges: {len(script_flow.edges)}")
print(f"  Trigger node present: {'trigger' in script_flow.nodes}")
print(f"  Entrypoint node present: {'entrypoint' in script_flow.nodes}")
has_load_items = any(node.name == "load_items" for node in script_flow.nodes.values())
has_normalize_item = any(node.name == "normalize_item" for node in script_flow.nodes.values())
has_batch_report = any(node.name == "BatchReport" for node in script_flow.nodes.values())
has_loop = any(node.node_type.value == "loop" for node in script_flow.nodes.values())
has_return = any(node.node_type.value == "return" for node in script_flow.nodes.values())
low_signal_unresolved = [
    node.id for node in script_flow.nodes.values()
    if node.id.startswith("unresolved.") and any(
        noise in node.id for noise in ("append", "strip", "upper")
    )
]
print(f"  load_items in flow: {has_load_items}")
print(f"  normalize_item in flow: {has_normalize_item}")
print(f"  BatchReport in flow: {has_batch_report}")
print(f"  loop logic present: {has_loop}")
print(f"  return logic present: {has_return}")
print(f"  low-signal unresolved nodes: {low_signal_unresolved}")
if not has_load_items:
    raise AssertionError("script flow did not include load_items()")
if not has_normalize_item:
    raise AssertionError("nested normalize_item() was not resolved in script flow")
if not has_batch_report:
    raise AssertionError("BatchReport constructor was not resolved in script flow")
if not has_loop or not has_return:
    raise AssertionError("script flow should expose loop/return logic nodes at Level 4")
if low_signal_unresolved:
    raise AssertionError(f"low-signal calls should be collapsed: {low_signal_unresolved}")
