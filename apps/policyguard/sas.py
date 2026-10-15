import httpx

from apps.policyguard.config import settings

INTEGRAL = "application/vnd.sas.business.rule.set.integral+json"


class SASClient:
    """All external SAS access passes through this HTTP adapter, including mock diagnostics."""

    def __init__(self, base_url=None, transport=None):
        self.client = httpx.Client(
            base_url=base_url or settings.sas_base_url, timeout=30, transport=transport,
            headers={"Authorization": f"Bearer {settings.sas_access_token}"} if settings.sas_access_token else {},
        )

    def get(self, path, integral=False):
        r = self.client.get(path, headers={"Accept": INTEGRAL if integral else "application/json"})
        r.raise_for_status()
        return r.json()

    def collection(self, path):
        items, start = [], 0
        while True:
            page = self.get(f"{path}?start={start}&limit=100")
            items.extend(page["items"])
            start += len(page["items"])
            if start >= page["count"] or not page["items"]:
                return items

    def post(self, path, body):
        r = self.client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def create_authoring(self, path, body):
        headers = {"X-Mock-Admin-Token": settings.mock_admin_token} if settings.sas_target == "simulator" else {}
        r = self.client.post(path, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    def edit_simulator_rule(self, rule_id, body):
        r = self.client.patch(
            f"/internal/rules/{rule_id}", json=body,
            headers={"X-Mock-Admin-Token": settings.mock_admin_token},
        )
        r.raise_for_status()
        return r.json()

    def apply_simulator_changes(self, body):
        r = self.client.post("/internal/rule-changes", json=body,
                             headers={"X-Mock-Admin-Token": settings.mock_admin_token})
        r.raise_for_status()
        return r.json()

    def score(self, inputs):
        r = self.client.post(
            "/microanalyticScore/modules/retail_transfer_fraud_controls/steps/execute",
            json={"inputs": [{"name": k, "value": v} for k, v in inputs.items()], "version": 1},
            headers={"Content-Type": "application/vnd.sas.microanalytic.module.step.input+json"},
        )
        r.raise_for_status()
        output = {v["name"]: v["value"] for v in r.json()["outputs"]}
        execution_id = r.headers.get("X-Mock-Execution-Id")
        return {
            "output": output,
            "execution_id": execution_id,
            "diagnostics": self.get(f"/internal/executions/{execution_id}") if execution_id else None,
            "raw_response": r.json(),
        }

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
