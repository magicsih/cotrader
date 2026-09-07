type Data = Record<string, any>;
type Api = (path: string, body?: unknown) => Promise<any>;

export const changedApproval =
  "설정이 변경되었습니다. 설정 확인·시작을 다시 눌러 최신 내용을 확인하세요.";

export function approvalMatches(reviewed: Data, current?: Data): boolean {
  return (
    !!current &&
    !current.pending_settings &&
    ["DRAFT", "PAUSED"].includes(current.status) &&
    reviewed.id === current.id &&
    reviewed.version === current.version &&
    reviewed.approval === current.approval
  );
}

export async function currentStartStrategy(
  api: Api,
  id: string,
): Promise<Data> {
  const current = (await api("/strategies")).find((row: Data) => row.id === id);
  if (!current) throw new Error("전략이 없습니다. 목록을 새로고침하세요.");
  if (current.pending_settings)
    throw new Error(
      "주의 설정을 반영 중입니다. 완료 후 설정 확인·시작을 눌러주세요.",
    );
  if (!["DRAFT", "PAUSED"].includes(current.status))
    throw new Error(
      "전략 상태가 변경되었습니다. 목록에서 현재 상태를 확인하세요.",
    );
  return current;
}

export async function verifyStartApproval(
  api: Api,
  reviewed: Data,
): Promise<void> {
  const current = await currentStartStrategy(api, reviewed.id);
  if (!approvalMatches(reviewed, current)) throw new Error(changedApproval);
}

export async function prepareStartApproval(api: Api, id: string) {
  const strategy = await currentStartStrategy(api, id);
  const preview = await api("/strategies/preview", {
    name: strategy.name,
    mode: strategy.mode,
    spec: strategy.config,
  });
  await verifyStartApproval(api, strategy);
  return { strategy, grid: preview.grid };
}

export async function waitForCommand(
  api: Api,
  id: string,
  sleep: () => Promise<void> = () =>
    new Promise((resolve) => setTimeout(resolve, 1000)),
): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt++) {
    const command = await api(`/commands/${encodeURIComponent(id)}`);
    if (command.status === "SUCCEEDED") return;
    if (!["QUEUED", "RUNNING"].includes(command.status))
      throw new Error(command.result?.message || "설정 변경에 실패했습니다.");
    await sleep();
  }
  throw new Error(
    "설정 변경을 아직 처리 중입니다. 처리 기록에서 완료 여부를 확인하세요.",
  );
}
