"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const panel = $("governor-panel");
  const toggle = $("governor-toggle");
  if (!panel || !toggle) return;

  const modes = {
    disabled: "꺼짐",
    ready: "연동 작업 대기",
    "gpu-pressure": "GPU 부하 대응",
    "cpu-preference": "CPU 분배 유지",
    waiting: "안전 여유 대기",
    active: "연동 작업 실행 중",
    orphaned: "실행 상태 확인 필요",
    fault: "분배 확인 필요",
  };
  let snapshot = null;
  let connected = false;
  let changing = false;
  let refreshing = false;
  let requestVersion = 0;
  let receivedAt = null;
  let actionError = "";
  let connectionError = "";
  let failedRequest = null;
  let adapterSignature = null;

  const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const string = (value, fallback = "—") => typeof value === "string" && value.trim() ? value : fallback;
  const finite = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const number = (value) => finite(value) ? value.toLocaleString("ko-KR", {maximumFractionDigits: 1}) : "—";
  const count = (value) => finite(value) && Number.isInteger(value) ? `${value.toLocaleString("ko-KR")}회` : "—";
  const write = (id, value) => {
    const node = $(id);
    if (node && node.textContent !== value) node.textContent = value;
  };
  const errorText = (error) => error?.name === "AbortError" ? "서버 응답 시간이 초과되었습니다." : string(error?.message, "서버에 연결할 수 없습니다.");

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch(path, {
        ...options,
        cache: "no-store",
        credentials: "same-origin",
        mode: "same-origin",
        signal: controller.signal,
      });
      let value;
      try {
        value = await response.json();
      } catch {
        throw new Error(`서버 응답을 읽을 수 없습니다. (HTTP ${response.status})`);
      }
      if (!response.ok) {
        const detail = string(object(value).error, "").slice(0, 300);
        throw new Error(`HTTP ${response.status}${detail ? ` · ${detail}` : ""}`);
      }
      return value;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function accept(value) {
    if (!value || typeof value.enabled !== "boolean" || Array.isArray(value)) {
      throw new Error("공통 분배 설정 응답의 형식을 확인할 수 없습니다.");
    }
    snapshot = value;
    connected = true;
    connectionError = "";
    receivedAt = new Date();
  }

  function renderAdapters(adapters) {
    const rows = Array.isArray(adapters) ? adapters.filter((row) => row && typeof row === "object") : [];
    const signature = JSON.stringify([Array.isArray(adapters), rows.map((row) => [row.stage, row.label, row.cpu_verified])]);
    if (signature === adapterSignature) return;
    adapterSignature = signature;
    const list = $("governor-adapters");
    list.replaceChildren();
    if (!rows.length) {
      const item = document.createElement("li");
      item.className = "subtle";
      item.textContent = Array.isArray(adapters) ? "등록된 연동 작업이 없습니다." : "연동 작업의 CPU 지원 정보를 확인할 수 없습니다.";
      list.append(item);
      return;
    }
    rows.forEach((adapter) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = string(adapter.label, string(adapter.stage, "연동 작업"));
      const support = document.createElement("span");
      const verified = adapter.cpu_verified === true;
      support.className = `governor-adapter-support${verified ? "" : " unavailable"}`;
      support.textContent = verified ? "CPU 경로 확인" : "CPU 분배 제외";
      item.append(label, support);
      list.append(item);
    });
  }

  function render() {
    panel.dataset.connected = String(connected);
    panel.setAttribute("aria-busy", String(changing));
    const enabled = snapshot?.enabled === true;
    toggle.disabled = changing || !connected || !snapshot;
    toggle.setAttribute("aria-checked", String(enabled));
    write("governor-toggle-label", changing ? "적용 중…" : !snapshot ? "설정 확인 중" : !connected ? "상태 확인 필요" : enabled ? "켜짐" : "꺼짐");
    const mode = connected ? string(snapshot?.mode, "unknown") : "unknown";
    write("governor-mode", changing ? "설정 적용 중" : !connected ? "연결 확인 중" : modes[mode] || "상태 미확인");
    $("governor-mode").className = `mini-label${Object.hasOwn(modes, mode) ? ` governor-mode-${mode}` : ""}`;
    let status = "공통 분배 설정을 불러오고 있습니다.";
    if (changing) status = "모든 연동 앱의 공통 정책을 적용하고 있습니다.";
    else if (connected) status = enabled ? "모든 연동 앱에 안정 분배가 켜져 있습니다." : "안정 분배가 꺼져 있습니다.";
    else if (snapshot) status = "연결을 다시 확인하고 있습니다. 아래 설정과 수치는 마지막 응답입니다.";
    else if (connectionError) status = "설정 상태를 확인할 수 없습니다. 자동으로 다시 확인합니다.";
    write("governor-status", status);
    write("governor-updated", receivedAt ? `${connected ? "수신" : "마지막 수신"} ${receivedAt.toLocaleTimeString("ko-KR", {hour12: false})} · 2초마다 확인` : "2초마다 확인");
    const error = [actionError, connectionError].filter(Boolean).join(" ");
    write("governor-error", error);
    $("governor-error").hidden = !error;
    if (!snapshot) return;

    const thresholds = object(snapshot.thresholds);
    write("governor-gpu-enter", `${number(thresholds.gpu_enter_pct)}% 이상`);
    write("governor-gpu-exit", `${number(thresholds.gpu_exit_pct)}% 이하 · ${number(thresholds.gpu_recovery_s)}초 안정`);
    write("governor-cpu-limit", `${number(thresholds.cpu_max_pct)}% 미만`);
    write("governor-ram-limit", `${number(thresholds.ram_max_pct)}% 미만`);
    write("governor-ram-free", `${number(thresholds.min_ram_available_gb)} GiB 이상`);
    const stats = object(snapshot.stats);
    write("governor-cpu-selected", count(stats.cpu_selected));
    write("governor-cpu-completed", count(stats.cpu_completed));
    write("governor-total-completed", `전체 완료 ${count(stats.completed)}`);

    const active = object(snapshot.active);
    if (snapshot.active && typeof active.stage === "string") {
      const adapters = Array.isArray(snapshot.adapters) ? snapshot.adapters : [];
      const adapter = adapters.find((row) => row?.stage === active.stage);
      const device = active.device === "cpu" ? "CPU" : active.device === "cuda" ? "GPU" : string(active.device, "장치 미확인");
      write("governor-active", `${string(adapter?.label, active.stage)} · ${device}${active.orphaned === true ? " · 실행 상태 확인 필요" : ""}`);
    } else if (snapshot.active === null) {
      write("governor-active", "실행 중인 연동 작업 없음");
    } else {
      write("governor-active", "연동 작업 상태 미확인");
    }
    write("governor-reason", string(snapshot.reason, "서버가 분배·대기 이유를 제공하지 않았습니다."));
    renderAdapters(snapshot.adapters);
  }

  async function refresh() {
    if (changing || refreshing) return;
    refreshing = true;
    const version = ++requestVersion;
    try {
      const value = await request("/api/governor");
      if (version === requestVersion) {
        accept(value);
        if (failedRequest !== null) {
          actionError = snapshot.enabled === failedRequest ? "" : "이전 설정 변경 요청이 현재 서버 설정에 반영되지 않았습니다. 다시 시도할 수 있습니다.";
          failedRequest = null;
        }
      }
    } catch (error) {
      if (version === requestVersion) {
        connected = false;
        connectionError = `상태를 불러오지 못했습니다. ${errorText(error)} 자동으로 다시 확인합니다.`;
      }
    } finally {
      refreshing = false;
      render();
    }
  }

  toggle.addEventListener("click", async () => {
    if (changing || !connected || !snapshot) return;
    const enabled = !snapshot.enabled;
    changing = true;
    ++requestVersion;
    actionError = "";
    connectionError = "";
    failedRequest = null;
    render();
    let submitted = false;
    try {
      const health = object(await request("/api/health"));
      if (typeof health.csrf_token !== "string" || !health.csrf_token.trim()) {
        throw new Error("설정 변경 인증 정보를 확인할 수 없습니다.");
      }
      await request("/api/governor/configure", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Resource-Analyzer-Token": health.csrf_token},
        body: JSON.stringify({enabled}),
      });
      submitted = true;
      accept(await request("/api/governor"));
      if (snapshot.enabled !== enabled) throw new Error("요청한 설정과 현재 서버 설정이 다릅니다. 현재 상태를 확인해 주세요.");
    } catch (error) {
      connected = false;
      failedRequest = enabled;
      actionError = `${submitted ? "설정 요청을 전송했으나 적용 상태를 확인하지 못했습니다." : "설정 변경을 확인하지 못했습니다."} ${errorText(error)}`;
    } finally {
      changing = false;
      render();
    }
  });

  async function poll() {
    await refresh();
    window.setTimeout(poll, 2000);
  }
  poll();
})();
