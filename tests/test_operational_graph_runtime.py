
import json
import subprocess
import unittest
from pathlib import Path


class OperationalGraphRuntimeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        js = (root / "flowlab/web/app.js").read_text()

        start = js.index("function buildOperationalTopology(")
        end = js.index("function drawStage(", start)

        cls.functions = js[start:end]

    def render(self, flows, roles, balances=None):
        payload = json.dumps({
            "flows": flows,
            "roles": roles,
            "balances": balances,
        })

        harness = """
const COLORS = {
  PLANNED: "#7C93A8",
  BROADCAST: "#2E86F5",
  CONFIRMED: "#38D39F"
};

let beads = [];

class Element {
  constructor(tag) {
    this.tag = tag;
    this.attrs = {};
    this.children = [];
    this.textContent = "";
  }

  setAttribute(key, value) {
    this.attrs[key] = String(value);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren() {
    this.children = [];
  }

  getTotalLength() {
    return 100;
  }
}

const stage = new Element("svg");

function $(id) {
  if (id !== "stage") throw Error("unexpected element");
  return stage;
}

function mk(tag, attrs, text) {
  const element = new Element(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    element.setAttribute(key, value);
  }
  if (text !== undefined) {
    element.textContent = String(text);
  }
  return element;
}

function dgb(value) {
  return (value / 1e8).toFixed(8);
}

function flatten(element) {
  return [
    {
      tag: element.tag,
      attrs: element.attrs,
      text: element.textContent
    },
    ...element.children.flatMap(flatten)
  ];
}
"""

        tail = """
const input = JSON.parse(INPUT_JSON);

drawOperationalStage(
  input.flows,
  input.balances,
  input.roles
);

const elements = flatten(stage);

console.log(JSON.stringify({
  elements,
  bead_count: beads.length,
  viewBox: stage.attrs.viewBox
}));
"""

        script = (
            harness
            + chr(10)
            + self.functions
            + chr(10)
            + "const INPUT_JSON = "
            + json.dumps(payload)
            + ";"
            + chr(10)
            + tail
        )

        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
        )

        if result.returncode:
            self.fail(result.stderr[:1500])

        return json.loads(result.stdout)

    def sample(self):
        roles = {
            "reserve": ["flab_source"],
            "stage": ["flab_stage"],
            "hubs": ["flab_hub_a"],
            "workers": ["flab_a"],
            "destinations": ["flab_destination"],
        }

        flows = [
            {
                "source": "flab_source",
                "wallets": ["flab_stage", "flab_hub_a"],
                "dest": "flab_destination",
                "jobs": [
                    {
                        "from": "flab_source",
                        "to": "flab_stage",
                        "seq": 0,
                        "state": "CONFIRMED",
                        "confs": 6,
                    },
                    {
                        "from": "flab_stage",
                        "to": "flab_hub_a",
                        "seq": 1,
                        "state": "BROADCAST",
                        "txid": "synthetic-tx-1",
                    },
                ],
            },
            {
                "source": "flab_hub_a",
                "wallets": ["flab_a"],
                "dest": "flab_destination",
                "jobs": [
                    {
                        "from": "flab_hub_a",
                        "to": "flab_a",
                        "seq": 0,
                        "state": "PLANNED",
                    },
                    {
                        "from": "flab_a",
                        "to": "flab_destination",
                        "seq": 1,
                        "state": "CONFIRMED",
                        "confs": 6,
                    },
                ],
            },
        ]

        return flows, roles

    def test_all_transaction_paths_render(self):
        flows, roles = self.sample()
        result = self.render(flows, roles)

        paths = [
            item for item in result["elements"]
            if item["tag"] == "path"
            and item["attrs"].get("stroke-width") is not None
        ]

        self.assertEqual(len(paths), 4)

    def test_five_wallet_nodes_render(self):
        flows, roles = self.sample()
        result = self.render(flows, roles)

        nodes = [
            item for item in result["elements"]
            if item["tag"] == "rect"
        ]

        self.assertEqual(len(nodes), 5)

    def test_role_headings_render(self):
        flows, roles = self.sample()
        result = self.render(flows, roles)

        headings = {
            item["text"]
            for item in result["elements"]
            if item["tag"] == "text"
        }

        for role in (
            "SOURCE",
            "STAGE",
            "HUBS",
            "WORKERS",
            "DESTINATIONS",
        ):
            self.assertIn(role, headings)

    def test_broadcast_animation_created(self):
        flows, roles = self.sample()
        result = self.render(flows, roles)

        self.assertEqual(result["bead_count"], 1)

    def test_planned_and_confirmed_edges_differ(self):
        flows, roles = self.sample()
        result = self.render(flows, roles)

        paths = [
            item for item in result["elements"]
            if item["tag"] == "path"
            and "opacity" in item["attrs"]
        ]

        planned = [
            path for path in paths
            if path["attrs"].get("stroke-dasharray") == "5 7"
        ]

        confirmed = [
            path for path in paths
            if path["attrs"].get("opacity") == "0.18"
        ]

        self.assertEqual(len(planned), 1)
        self.assertEqual(len(confirmed), 2)

    def test_graph_has_valid_viewbox(self):
        flows, roles = self.sample()
        result = self.render(flows, roles)

        self.assertEqual(
            len(result["viewBox"].split()),
            4,
        )

    def test_no_job_no_transaction_edge(self):
        result = self.render(
            [{
                "source": "flab_source",
                "dest": "flab_destination",
                "jobs": [],
            }],
            {
                "reserve": ["flab_source"],
                "destinations": ["flab_destination"],
            },
        )

        paths = [
            item for item in result["elements"]
            if item["tag"] == "path"
            and "opacity" in item["attrs"]
        ]

        self.assertEqual(paths, [])
        self.assertEqual(result["bead_count"], 0)



    def test_settlement_operation_renders(self):
        result = self.render([{"jobs": [{
            "from": "flab_stage",
            "to": None,
            "to_label": "Settlement (3 destinations)",
            "seq": 4,
            "state": "BROADCAST",
            "txid": "synthetic-settlement",
        }]}], {"stage": ["flab_stage"]})

        items = result["elements"]
        operations = [
            item for item in items
            if item["attrs"].get("data-operation") == "settlement"
        ]

        self.assertEqual(len(operations), 2)
        self.assertEqual(result["bead_count"], 1)
        self.assertTrue(any(
            item["text"] == "Settlement (3 destinations)"
            for item in items
        ))
        self.assertEqual(len([
            item for item in items if item["tag"] == "rect"
        ]), 2)

if __name__ == "__main__":
    unittest.main()
