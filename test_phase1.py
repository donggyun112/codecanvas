"""Quick test for Phase 1: Static endpoint map."""
import json
import sys
sys.path.insert(0, "core")

from codecanvas.parser.fastapi_extractor import FastAPIExtractor
from codecanvas.parser.call_graph import CallGraphBuilder
from codecanvas.graph.builder import FlowGraphBuilder

SAMPLE_PROJECT = "./sample-fastapi"

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

# 2. Build flow for POST /login
print("\n--- Flow Graph for POST /api/v1/auth/login ---")
builder = FlowGraphBuilder(SAMPLE_PROJECT)
builder.get_endpoints()

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
