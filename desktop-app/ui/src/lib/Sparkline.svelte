<script lang="ts">
  interface Props {
    points: number[];
    color?: string;
    height?: number;
  }
  let { points = [], color = '#38bdf8', height = 46 }: Props = $props();

  const padded = $derived.by(() => {
    const target = 60;
    if (points.length >= target) return points.slice(-target);
    return [...new Array(target - points.length).fill(0), ...points];
  });

  const maxVal = $derived(Math.max(1024 * 512, ...padded));

  const linePath = $derived.by(() => {
    const w = 600;
    const h = height;
    return padded
      .map((v, i) => {
        const x = (i / (padded.length - 1)) * w;
        const y = h - (Math.min(1, v / maxVal) * (h - 6)) - 3;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  });

  const areaPath = $derived(`${linePath} L600,${height} L0,${height} Z`);
</script>

<svg
  viewBox="0 0 600 {height}"
  preserveAspectRatio="none"
  class="sparkline"
  style="height: {height}px;"
  aria-label="60s throughput sparkline"
>
  <defs>
    <linearGradient id="spark-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color={color} stop-opacity="0.28" />
      <stop offset="100%" stop-color={color} stop-opacity="0.02" />
    </linearGradient>
  </defs>
  <line x1="0" y1={height * 0.25} x2="600" y2={height * 0.25} stroke="rgba(255,255,255,0.05)" stroke-dasharray="4 4" />
  <line x1="0" y1={height * 0.5} x2="600" y2={height * 0.5} stroke="rgba(255,255,255,0.05)" stroke-dasharray="4 4" />
  <line x1="0" y1={height * 0.75} x2="600" y2={height * 0.75} stroke="rgba(255,255,255,0.05)" stroke-dasharray="4 4" />
  <path d={areaPath} fill="url(#spark-grad)" />
  <path d={linePath} fill="none" stroke={color} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
</svg>

<style>
  .sparkline {
    width: 100%;
    display: block;
  }
</style>
