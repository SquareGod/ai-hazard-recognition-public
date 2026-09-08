"use client";

import ManagedStreamPlayer from "@/components/managed-stream-player";

type Props = {
  webrtcUrl: string;
  hlsUrl: string;
  title?: string;
};

export default function StreamPlayer({ webrtcUrl, hlsUrl, title = "摄像头实时画面" }: Props) {
  return <ManagedStreamPlayer camera={{ id: title, name: title, webrtcUrl, hlsUrl }} />;
}
