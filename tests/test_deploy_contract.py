from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_deployer_can_patch_only_the_named_runtime_configmap():
    role = (ROOT / "deploy/k8s/ci-deployer.yaml").read_text()
    assert "resources: [configmaps]" in role
    assert "resourceNames: [cotrader-config]" in role
    assert "verbs: [get, patch]" in role
    assert "resources: [secrets]" not in role


def test_deploy_requires_and_patches_the_three_approved_environment_values():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    for name in (
        "COTRADER_DAILY_LOSS_USDT",
        "COTRADER_DRAWDOWN_USDT",
        "COTRADER_UPBIT_USDT_AUTO_RECOVER",
    ):
        assert f"vars.{name}" in workflow
        assert f'"{name}"' in workflow
    assert "patch configmap cotrader-config --type merge" in workflow
