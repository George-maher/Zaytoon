import { memo, useEffect, useRef, useMemo } from "react";

/**
 * Waveform - Audio visualizer for listening/speaking states
 * Renders real-time audio waveform when listening or speaking
 */
export default memo(function Waveform({ analyserNode = null, isListening = false, isSpeaking = false }) {
  const canvasRef = useRef(null);
  const animRef = useRef(null);
  const dataArrayRef = useRef(null);

  const color = useMemo(() => {
    if (isSpeaking) return "#00F0FF";
    if (isListening) return "#00FF88";
    return "#00D4FF";
  }, [isListening, isSpeaking]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width;
    const H = canvas.height;

    const drawIdleWave = (ctx, W, H, color) => {
      const t = Date.now() / 1000;
      ctx.lineWidth = 1;
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.2;
      ctx.beginPath();
      for (let x = 0; x < W; x++) {
        const y = H / 2 + Math.sin(x * 0.02 + t * 0.8) * 4 + Math.sin(x * 0.05 + t * 1.2) * 2;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    };

    const draw = () => {
      animRef.current = requestAnimationFrame(draw);
      if (!ctx) return;
      ctx.clearRect(0, 0, W, H);

      if (!analyserNode || (!isListening && !isSpeaking)) {
        drawIdleWave(ctx, W, H, color);
        return;
      }

      if (!dataArrayRef.current) {
        dataArrayRef.current = new Uint8Array(analyserNode.frequencyBinCount);
      }
      analyserNode.getByteTimeDomainData(dataArrayRef.current);
      const data = dataArrayRef.current;

      ctx.lineWidth = 1.5;
      ctx.strokeStyle = color;
      ctx.shadowColor = color;
      ctx.shadowBlur = 8;
      ctx.beginPath();

      const sliceWidth = W / data.length;
      let x = 0;
      for (let i = 0; i < data.length; i++) {
        const v = data[i] / 128.0;
        const y = (v * H) / 2;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
        x += sliceWidth;
      }
      ctx.lineTo(W, H / 2);
      ctx.stroke();
      ctx.shadowBlur = 0;

      // Mirror with lower opacity
      ctx.globalAlpha = 0.2;
      ctx.beginPath();
      x = 0;
      for (let i = 0; i < data.length; i++) {
        const v = data[i] / 128.0;
        const y = H - (v * H) / 2;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
        x += sliceWidth;
      }
      ctx.lineTo(W, H / 2);
      ctx.stroke();
      ctx.globalAlpha = 1;
    };

    draw();
    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current);
    };
  }, [analyserNode, isListening, isSpeaking, color]);

  return (
    <canvas
      ref={canvasRef}
      width={300}
      height={60}
      className="w-[300px] h-[60px]"
      style={{ opacity: isListening || isSpeaking ? 1 : 0.3 }}
    />
  );
});