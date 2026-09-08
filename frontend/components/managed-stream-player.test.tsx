import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ManagedStreamPlayer from "@/components/managed-stream-player";
import { connectWhep } from "@/lib/whep-client";
import type { WhepConnection } from "@/lib/whep-client";

const hlsInstances: Array<{
  loadSource: ReturnType<typeof vi.fn>;
  attachMedia: ReturnType<typeof vi.fn>;
  destroy: ReturnType<typeof vi.fn>;
  startLoad: ReturnType<typeof vi.fn>;
  handlers: Record<string, (event: string, data: { fatal: boolean; type: string }) => void>;
}> = [];

vi.mock("@/lib/whep-client", () => ({ connectWhep: vi.fn() }));
vi.mock("hls.js", () => ({
  default: class MockHls {
    static Events = { ERROR: "hlsError" };
    static ErrorTypes = { NETWORK_ERROR: "networkError", MEDIA_ERROR: "mediaError" };
    static isSupported() { return true; }
    loadSource = vi.fn();
    attachMedia = vi.fn();
    destroy = vi.fn();
    startLoad = vi.fn();
    handlers: Record<string, (event: string, data: { fatal: boolean; type: string }) => void> = {};
    on = vi.fn((event: string, handler: (event: string, data: { fatal: boolean; type: string }) => void) => { this.handlers[event] = handler; });
    constructor() { hlsInstances.push(this); }
  },
}));

const camera = {
  id: "cam-01",
  name: "A1北侧临边",
  webrtcUrl: "/api/v1/media/ticket/cam-01/whep",
  hlsUrl: "/api/v1/media/ticket/cam-01/hls/index.m3u8",
};

function mockWhepSuccess(connection: WhepConnection) {
  vi.mocked(connectWhep).mockImplementation(async (video) => {
    queueMicrotask(() => video.dispatchEvent(new Event("playing")));
    return connection;
  });
}

describe("ManagedStreamPlayer", () => {
  beforeEach(() => {
    vi.mocked(connectWhep).mockReset();
    hlsInstances.length = 0;
    window.sessionStorage.clear();
  });

  it("优先建立WHEP并显示实时播放状态", async () => {
    mockWhepSuccess({ close: vi.fn(async () => undefined) });
    render(<ManagedStreamPlayer camera={camera} />);

    await waitFor(() => expect(connectWhep).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("实时播放中")).toBeInTheDocument();
    expect(screen.getByLabelText("A1北侧临边实时画面")).toBeInTheDocument();
  });

  it("WHEP连续失败两次后自动回退HLS", async () => {
    vi.mocked(connectWhep).mockRejectedValue(new Error("network"));
    render(<ManagedStreamPlayer camera={camera} />);

    await waitFor(() => expect(connectWhep).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("已切换兼容 HLS")).toBeInTheDocument();
    expect(hlsInstances).toHaveLength(1);
    expect(hlsInstances[0].loadSource).toHaveBeenCalledWith(camera.hlsUrl);
  });

  it("连接失败期间显示重连状态而不是静默黑屏", async () => {
    let rejectSecond: ((error: Error) => void) | undefined;
    vi.mocked(connectWhep)
      .mockRejectedValueOnce(new Error("first"))
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectSecond = reject; }));
    render(<ManagedStreamPlayer camera={camera} />);

    expect(await screen.findByText("正在重连（2/2）")).toBeInTheDocument();
    rejectSecond?.(new Error("second"));
  });

  it("保留WebRTC、HLS和重新加载控制", async () => {
    mockWhepSuccess({ close: vi.fn(async () => undefined) });
    render(<ManagedStreamPlayer camera={camera} />);

    expect(screen.getByRole("button", { name: "低延迟 WebRTC" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "兼容 HLS" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "兼容 HLS" }));
    await waitFor(() => expect(hlsInstances).toHaveLength(1));
  });

  it("卸载时关闭WHEP连接", async () => {
    const close = vi.fn(async () => undefined);
    mockWhepSuccess({ close });
    const view = render(<ManagedStreamPlayer camera={camera} />);
    await screen.findByText("实时播放中");

    view.unmount();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("WebRTC建立后断流会持续重新建立连接", async () => {
    vi.useFakeTimers();
    let disconnected: (() => void) | undefined;
    mockWhepSuccess({
      close: vi.fn(async () => undefined),
      onDisconnected: (callback: () => void) => { disconnected = callback; return () => { disconnected = undefined; }; },
    });
    render(<ManagedStreamPlayer camera={camera} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(connectWhep).toHaveBeenCalledTimes(1);

    act(() => disconnected?.());
    await act(async () => { vi.advanceTimersByTime(1_000); await Promise.resolve(); await Promise.resolve(); });
    expect(connectWhep).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("HLS网络错误有限指数重试后显示离线", async () => {
    vi.useFakeTimers();
    mockWhepSuccess({ close: vi.fn(async () => undefined) });
    render(<ManagedStreamPlayer camera={camera} />);
    fireEvent.click(screen.getByRole("button", { name: "兼容 HLS" }));
    await vi.waitFor(() => expect(hlsInstances).toHaveLength(1));
    const instance = hlsInstances[0];

    act(() => instance.handlers.hlsError("hlsError", { fatal: true, type: "networkError" }));
    expect(screen.getByText("正在恢复兼容视频（1/3）")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(1000);
    expect(instance.startLoad).toHaveBeenCalledTimes(1);

    act(() => instance.handlers.hlsError("hlsError", { fatal: true, type: "networkError" }));
    await vi.advanceTimersByTimeAsync(2000);
    act(() => instance.handlers.hlsError("hlsError", { fatal: true, type: "networkError" }));
    await vi.advanceTimersByTimeAsync(4000);
    act(() => instance.handlers.hlsError("hlsError", { fatal: true, type: "networkError" }));
    expect(screen.getByText("视频暂时不可用，请检查视频源")).toBeInTheDocument();
    vi.useRealTimers();
  });

  it("浏览器原生HLS断流后会自动重新加载", async () => {
    vi.useFakeTimers();
    const canPlayType = vi.spyOn(HTMLMediaElement.prototype, "canPlayType").mockReturnValue("probably");
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    vi.mocked(connectWhep).mockImplementation(() => new Promise(() => undefined));
    render(<ManagedStreamPlayer camera={camera} />);

    fireEvent.click(screen.getByRole("button", { name: "兼容 HLS" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(play).toHaveBeenCalledTimes(1);
    fireEvent.error(screen.getByLabelText("A1北侧临边实时画面"));
    expect(screen.getByText("正在恢复兼容视频（0/3）")).toBeInTheDocument();

    await act(async () => { vi.advanceTimersByTime(2_000); await Promise.resolve(); await Promise.resolve(); });
    expect(play).toHaveBeenCalledTimes(2);
    canPlayType.mockRestore();
    play.mockRestore();
    load.mockRestore();
    vi.useRealTimers();
  });
});
