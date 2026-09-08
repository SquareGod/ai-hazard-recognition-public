import { beforeEach, describe, expect, it, vi } from "vitest";

import { connectWhep } from "@/lib/whep-client";

class FakePeerConnection {
  static latest: FakePeerConnection;
  iceGatheringState = "complete";
  localDescription = { type: "offer", sdp: "offer-sdp" };
  ontrack: ((event: { streams: MediaStream[] }) => void) | null = null;
  close = vi.fn();
  addTransceiver = vi.fn();
  createOffer = vi.fn().mockResolvedValue(this.localDescription);
  setLocalDescription = vi.fn().mockResolvedValue(undefined);
  setRemoteDescription = vi.fn().mockResolvedValue(undefined);
  addEventListener = vi.fn();
  removeEventListener = vi.fn();
  constructor() { FakePeerConnection.latest = this; }
}

describe("connectWhep", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
  });

  it("发送非trickle SDP并在关闭时删除不透明会话", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response("answer-sdp", {
        status: 201,
        headers: { Location: "/api/v1/media/session/opaque" },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const video = document.createElement("video");

    const connection = await connectWhep(video, "/api/v1/media/ticket/cam/whep");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/media/ticket/cam/whep", expect.objectContaining({ method: "POST", body: "offer-sdp", credentials: "same-origin" }));
    const request = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(request.headers).get("Content-Type")).toBe("application/sdp");
    expect(new Headers(request.headers).get("X-Project-ID")).toBeTruthy();
    expect(FakePeerConnection.latest.setRemoteDescription).toHaveBeenCalledWith({ type: "answer", sdp: "answer-sdp" });

    await connection.close();
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/media/session/opaque", expect.objectContaining({ method: "DELETE", credentials: "same-origin" }));
    expect(new Headers((fetchMock.mock.calls[1][1] as RequestInit).headers).get("X-Project-ID")).toBeTruthy();
    expect(FakePeerConnection.latest.close).toHaveBeenCalled();
  });

  it("信令失败时关闭连接并返回可显示错误", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("denied", { status: 401 })));
    const video = document.createElement("video");

    await expect(connectWhep(video, "/api/v1/media/ticket/cam/whep")).rejects.toThrow("WHEP信令失败（401）");
    expect(FakePeerConnection.latest.close).toHaveBeenCalled();
  });
});
