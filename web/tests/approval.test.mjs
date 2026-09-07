import assert from "node:assert/strict";
import test from "node:test";
import {
  approvalMatches,
  prepareStartApproval,
  verifyStartApproval,
  waitForCommand,
} from "../src/approval.ts";

const strategy = (version = 1) => ({
  id: "fixture",
  version,
  approval: `approval-${version}`,
  status: "DRAFT",
  name: "fixture",
  mode: "paper",
  config: {
    allowed_market_cautions: version > 1 ? ["TRADING_VOLUME_SOARING"] : [],
  },
});

test("delayed setting command finishes before a fresh preview is prepared", async () => {
  let current = strategy();
  let polls = 0;
  let previews = 0;
  const api = async (path, body) => {
    if (path === "/commands/fixture") {
      polls++;
      if (polls === 3) current = strategy(2);
      return { status: polls < 3 ? "QUEUED" : "SUCCEEDED" };
    }
    if (path === "/strategies") return [current];
    assert.equal(path, "/strategies/preview");
    assert.deepEqual(body.spec.allowed_market_cautions, [
      "TRADING_VOLUME_SOARING",
    ]);
    previews++;
    return { grid: ["new-grid"] };
  };
  await waitForCommand(api, "fixture", async () => {});
  const prepared = await prepareStartApproval(api, "fixture");
  assert.equal(polls, 3);
  assert.equal(previews, 1);
  assert.equal(prepared.strategy.version, 2);
  assert.deepEqual(prepared.grid, ["new-grid"]);
});

test("pending settings survive reload and prevent approval preview", async () => {
  const api = async (path) => {
    assert.equal(path, "/strategies");
    return [{ ...strategy(), pending_settings: true }];
  };
  await assert.rejects(prepareStartApproval(api, "fixture"), /반영 중/);
});

test("a version changed during preview must be reviewed again", async () => {
  let version = 1;
  const api = async (path) => {
    if (path === "/strategies") return [strategy(version)];
    assert.equal(path, "/strategies/preview");
    version++;
    return { grid: [] };
  };
  await assert.rejects(prepareStartApproval(api, "fixture"), /최신 내용/);
});

for (const [name, current] of Object.entries({
  version: strategy(2),
  risk: { ...strategy(), approval: "changed-risk" },
  pending: { ...strategy(), pending_settings: true },
  running: { ...strategy(), status: "RUNNING" },
  missing: undefined,
})) {
  test(`final confirmation rejects ${name} change without sending a command`, async () => {
    assert.equal(approvalMatches(strategy(), current), false);
    await assert.rejects(
      verifyStartApproval(async (path, body) => {
        assert.equal(path, "/strategies");
        assert.equal(body, undefined);
        return current ? [current] : [];
      }, strategy()),
    );
  });
}

test("an unchanged reviewed approval passes without being replaced", async () => {
  const reviewed = strategy();
  assert.equal(approvalMatches(reviewed, strategy()), true);
  await verifyStartApproval(async () => [strategy()], reviewed);
  assert.deepEqual(reviewed, strategy());
});

test("rejected or unresolved saves are never reported as complete or resubmitted", async () => {
  await assert.rejects(
    waitForCommand(
      async () => ({
        status: "REJECTED",
        result: { message: "fixture rejection" },
      }),
      "fixture",
    ),
    /fixture rejection/,
  );
  let polls = 0;
  await assert.rejects(
    waitForCommand(
      async (path, body) => {
        assert.equal(path, "/commands/fixture");
        assert.equal(body, undefined);
        polls++;
        return { status: "QUEUED" };
      },
      "fixture",
      async () => {},
    ),
    /아직 처리 중/,
  );
  assert.equal(polls, 30);
});
