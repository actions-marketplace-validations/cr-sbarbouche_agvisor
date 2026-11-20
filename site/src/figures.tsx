// Illustrations. The recurring shape is the orbit: a node is a disc inside a dashed ring,
// and a gold satellite marks what is live. One SVG per figure, so each scales as a whole.
import type { ComponentType } from 'react';
import { Bot, Camera, Files, Gauge, Github, HardDrive, History, Layers, Package, Plus, ShieldCheck, Terminal, Timer, Trash2 } from 'lucide-react';

const NAVY = '#34336b'; // icon strokes
const LINK = '#8587a4'; // connectors
const GOLD = '#d6a944';

// ponytail: tiny seeded PRNG so the scenery is identical on every render and every build.
function rng(seed: number) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Icon = ComponentType<{ x?: number; y?: number; width?: number; height?: number; stroke?: string; strokeWidth?: number }>;

export function FigureDefs() {
  return (
    <svg width="0" height="0" style={{ position: 'absolute' }} aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="nodeFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fff" />
          <stop offset="1" stopColor="#f1f3fa" />
        </linearGradient>
        <filter id="nodeShadow" x="-30%" y="-30%" width="160%" height="170%">
          <feDropShadow dx="0" dy="8" stdDeviation="7" floodColor="#29284f" floodOpacity=".14" />
        </filter>
      </defs>
    </svg>
  );
}

// A disc inside a dashed orbit ring. `live` adds the gold satellite.
function Node({ cx, cy, icon: I, label, live, ring = 54, labelY }: { cx: number; cy: number; icon: Icon; label?: string; live?: boolean; ring?: number; labelY?: number }) {
  const k = ring * Math.SQRT1_2;
  return (
    <g>
      <circle cx={cx} cy={cy} r={ring} fill="none" stroke="#c3c6dc" strokeDasharray="3 5" />
      <circle cx={cx} cy={cy} r={42} fill="url(#nodeFill)" stroke="#cdd0e3" filter="url(#nodeShadow)" />
      <I x={cx - 19} y={cy - 19} width={38} height={38} stroke={NAVY} strokeWidth={1.4} />
      {live && <circle cx={cx + k} cy={cy - k} r={5.5} fill={GOLD} stroke="#fff" strokeWidth={2} />}
      {label && <text x={cx} y={labelY ?? cy + ring + 26} textAnchor="middle" className="fig-label">{label}</text>}
    </g>
  );
}

function Arrow({ x1, x2, y }: { x1: number; x2: number; y: number }) {
  return (
    <g stroke={LINK} strokeWidth={1.3} fill="none" strokeLinecap="round" strokeLinejoin="round">
      <path d={`M${x1} ${y}H${x2}`} strokeDasharray="2 5" />
      <path d={`M${x2 - 7} ${y - 5}L${x2} ${y}L${x2 - 7} ${y + 5}`} stroke={GOLD} strokeWidth={1.6} />
    </g>
  );
}

// A data volume: a cylinder with two gold bands.
function Volume({ cx, cy, label, labelY }: { cx: number; cy: number; label?: string; labelY?: number }) {
  const rx = 50, ry = 15, h = 66;
  const band = (y: number) => <path key={y} d={`M${cx - rx} ${y}A${rx} ${ry} 0 0 0 ${cx + rx} ${y}`} fill="none" stroke={GOLD} strokeWidth={1.5} />;
  return (
    <g>
      <path d={`M${cx - rx} ${cy - h / 2}V${cy + h / 2}A${rx} ${ry} 0 0 0 ${cx + rx} ${cy + h / 2}V${cy - h / 2}`} fill="url(#nodeFill)" stroke="#cdd0e3" filter="url(#nodeShadow)" />
      {band(cy - h / 6)}
      {band(cy + h / 6)}
      <ellipse cx={cx} cy={cy - h / 2} rx={rx} ry={ry} fill="#fff" stroke="#cdd0e3" />
      <path d={`M${cx - 20} ${cy - h / 2 - 3}L${cx + 14} ${cy - h / 2 + 5}`} stroke={NAVY} strokeWidth={1.4} strokeLinecap="round" />
      {label && <text x={cx} y={labelY ?? cy + 80} textAnchor="middle" className="fig-label">{label}</text>}
    </g>
  );
}

function Legend({ x, y, icon: I, text }: { x: number; y: number; icon: Icon; text: string }) {
  return (
    <g>
      <I x={x} y={y} width={22} height={22} stroke={NAVY} strokeWidth={1.4} />
      <text x={x + 32} y={y + 16} className="fig-sans">{text}</text>
    </g>
  );
}

// Session 1 -> /workspace -> Session 2
export function WorkspaceFlow() {
  return (
    <svg viewBox="0 0 540 156" role="img" aria-label="Two sessions sharing one persistent workspace volume">
      <Node cx={70} cy={62} icon={Terminal} live label="Session 1" />
      <Arrow x1={134} x2={208} y={62} />
      <Volume cx={270} cy={62} label="/workspace" labelY={140} />
      <Arrow x1={332} x2={406} y={62} />
      <Node cx={470} cy={62} icon={Terminal} live label="Session 2" />
    </svg>
  );
}

// Two entry points, one runner.
export function CiMerge() {
  return (
    <svg viewBox="0 0 520 236" role="img" aria-label="A GitHub Action step and your own machine both run the same agvisor sandbox">
      <Node cx={110} cy={62} icon={Github} label="GitHub Action" />
      <Node cx={410} cy={62} icon={Terminal} live label="Your machine" />
      <g stroke={LINK} strokeWidth={1.3} fill="none" strokeLinecap="round" strokeDasharray="2 5">
        <path d="M110 152V176H410V152M260 176V200" />
      </g>
      <Legend x={186} y={206} icon={Layers} text="agvisor run" />
    </svg>
  );
}

// /workspace -> snapshot() | restore()
export function SnapshotSplit() {
  return (
    <svg viewBox="0 0 520 236" role="img" aria-label="A named sandbox's workspace can be snapshotted and restored">
      <Node cx={260} cy={62} icon={HardDrive} live label="/workspace" />
      <g stroke={LINK} strokeWidth={1.3} fill="none" strokeLinecap="round" strokeLinejoin="round">
        <path d="M260 152V174M170 174H350M170 174V196M350 174V196" strokeDasharray="2 5" />
        <path d="M165 190L170 197L175 190M345 190L350 197L355 190" stroke={GOLD} strokeWidth={1.6} />
      </g>
      <Legend x={130} y={204} icon={Camera} text="snapshot()" />
      <Legend x={314} y={204} icon={History} text="restore()" />
    </svg>
  );
}

// Create -> Work -> Delete
export function Lifecycle() {
  return (
    <svg viewBox="0 0 540 156" role="img" aria-label="A sandbox is created, used, then deleted">
      <Node cx={70} cy={62} icon={Plus} label="Create" />
      <Arrow x1={134} x2={208} y={62} />
      <Node cx={270} cy={62} icon={Terminal} live label="Work" />
      <Arrow x1={334} x2={408} y={62} />
      <Node cx={470} cy={62} icon={Trash2} label="Delete" />
    </svg>
  );
}

// An allowlist proxy in front of two sandboxes, each inside its own network (the wider dashed ring).
export function NetworkIsolation() {
  return (
    <svg viewBox="0 0 540 214" role="img" aria-label="An allowlist proxy connects to two sandboxes, each on its own private network">
      <g stroke={LINK} strokeWidth={1.3} fill="none" strokeLinejoin="round" strokeDasharray="2 5">
        <path d="M128 112H182V12H450V30M300 12V30" />
      </g>
      <circle cx={300} cy={12} r={3} fill={GOLD} />
      <Node cx={70} cy={112} icon={ShieldCheck} live label="Allowlist proxy" />
      <Node cx={300} cy={112} icon={Terminal} live ring={76} label="Sandbox" labelY={206} />
      <Node cx={450} cy={112} icon={Terminal} live ring={76} label="Sandbox" labelY={206} />
    </svg>
  );
}

// A sandbox cube with its capabilities in orbit.
const orbitNodes: { a: number; icon: Icon; label: string; hot?: boolean }[] = [
  { a: -90, icon: Package, label: 'image' },
  { a: -30, icon: Gauge, label: 'limits' },
  { a: 30, icon: ShieldCheck, label: 'egress' },
  { a: 90, icon: Files, label: 'files' },
  { a: 150, icon: Timer, label: 'jobs' },
  { a: 210, icon: Bot, label: 'agents', hot: true },
];
const rad = (d: number) => (d * Math.PI) / 180;

// An isometric cube centred on (cx, cy), edge s. Gold top, glass sides.
function Cube({ cx, cy, s, dark }: { cx: number; cy: number; s: number; dark?: boolean }) {
  const w = s * 0.866;
  const p = (dx: number, dy: number) => `${cx + dx * w},${cy + dy * s * 0.5}`;
  const top = [p(0, -2), p(1, -1), p(0, 0), p(-1, -1)].join(' ');
  const left = [p(-1, -1), p(0, 0), p(0, 2), p(-1, 1)].join(' ');
  const right = [p(1, -1), p(1, 1), p(0, 2), p(0, 0)].join(' ');
  const stroke = dark ? '#e8c46d' : NAVY;
  return (
    <g strokeLinejoin="round" strokeWidth={1.5} stroke={stroke}>
      <polygon points={left} fill={dark ? '#ffffff0f' : '#34336b14'} />
      <polygon points={right} fill={dark ? '#ffffff1f' : '#34336b26'} />
      <polygon points={top} fill={dark ? '#e8c46d38' : '#e8c46d99'} />
    </g>
  );
}

export function OrbitMap() {
  const C = 300;
  return (
    <svg viewBox="0 0 600 600" role="img" aria-label="A sandbox with an image, limits, egress rules, files, jobs and agents in orbit around it">
      <circle cx={C} cy={C} r={118} fill="none" stroke="#c3c6dc" strokeDasharray="3 6" />
      <circle cx={C} cy={C} r={215} fill="none" stroke="#d3d6e6" />
      <Cube cx={C} cy={C + 4} s={62} />
      {[-60, 40, 200].map((a) => (
        <circle key={a} cx={C + 118 * Math.cos(rad(a))} cy={C + 118 * Math.sin(rad(a))} r={5} fill={GOLD} stroke="#fff" strokeWidth={2} />
      ))}
      {orbitNodes.map((n) => {
        const x = C + 215 * Math.cos(rad(n.a));
        const y = C + 215 * Math.sin(rad(n.a));
        const ix = C + 122 * Math.cos(rad(n.a));
        const iy = C + 122 * Math.sin(rad(n.a));
        const ox = C + (215 - 38) * Math.cos(rad(n.a));
        const oy = C + (215 - 38) * Math.sin(rad(n.a));
        return (
          <g key={n.label}>
            <path d={`M${ix} ${iy}L${ox} ${oy}`} stroke={n.hot ? GOLD : '#c3c6dc'} strokeWidth={1.6} strokeDasharray="4 5" />
            <circle cx={x} cy={y} r={36} fill="url(#nodeFill)" stroke={n.hot ? GOLD : '#cdd0e3'} strokeWidth={n.hot ? 1.6 : 1} filter="url(#nodeShadow)" />
            <n.icon x={x - 14} y={y - 14} width={28} height={28} stroke={NAVY} strokeWidth={1.4} />
            <text x={x} y={y + 58} textAnchor="middle" className="fig-mono">{n.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

// The hero: a night sky, rings of orbit, a sandbox cube and a few satellites.
const stars = (() => {
  const r = rng(3);
  return Array.from({ length: 150 }, () => ({ x: r() * 1440, y: r() * 760, r: 0.5 + r() * 1.3, o: 0.15 + r() * 0.6 }));
})();
const HX = 1040;
const HY = 330;
const rings = [
  { r: 130, dash: '2 7', o: 0.38 },
  { r: 215, dash: '', o: 0.16 },
  { r: 310, dash: '3 9', o: 0.22 },
  { r: 420, dash: '', o: 0.11 },
  { r: 540, dash: '2 11', o: 0.14 },
];
const sats = [
  { ring: 0, a: -35, gold: true }, { ring: 1, a: 200, gold: false }, { ring: 1, a: 20, gold: true },
  { ring: 2, a: -120, gold: false }, { ring: 2, a: 70, gold: true }, { ring: 3, a: -20, gold: false },
  { ring: 3, a: 150, gold: true }, { ring: 4, a: -80, gold: false },
];

export function HeroScene() {
  return (
    <svg className="hero-scene" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="night" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#12112b" />
          <stop offset=".5" stopColor="#1d1c42" />
          <stop offset="1" stopColor="#2c2b58" />
        </linearGradient>
        <radialGradient id="glow" cx={HX / 1440} cy={HY / 900} r=".42" gradientUnits="objectBoundingBox">
          <stop offset="0" stopColor="#e8c46d" stopOpacity=".34" />
          <stop offset=".45" stopColor="#e8c46d" stopOpacity=".07" />
          <stop offset="1" stopColor="#e8c46d" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="scrimL" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#0d0c22" stopOpacity=".6" />
          <stop offset=".55" stopColor="#0d0c22" stopOpacity=".18" />
          <stop offset="1" stopColor="#0d0c22" stopOpacity="0" />
        </linearGradient>
        <filter id="blur6"><feGaussianBlur stdDeviation="6" /></filter>
        <filter id="grain" x="0" y="0" width="100%" height="100%">
          <feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" stitchTiles="stitch" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
      </defs>
      <rect width="1440" height="900" fill="url(#night)" />
      <rect width="1440" height="900" fill="url(#glow)" />
      {stars.map((s, i) => <circle key={i} cx={s.x} cy={s.y} r={s.r} fill="#fff" opacity={s.o} />)}
      {rings.map((g) => <circle key={g.r} cx={HX} cy={HY} r={g.r} fill="none" stroke="#fff" strokeOpacity={g.o} strokeDasharray={g.dash || undefined} />)}
      <path d={`M${HX - 215} ${HY}A215 215 0 0 1 ${HX} ${HY - 215}`} fill="none" stroke="#e8c46d" strokeOpacity=".55" strokeWidth="1.6" />
      <ellipse cx={HX} cy={HY + 58} rx="86" ry="22" fill="#e8c46d" opacity=".35" filter="url(#blur6)" />
      <Cube cx={HX} cy={HY} s={74} dark />
      {sats.map((s, i) => {
        const r = rings[s.ring].r;
        const x = HX + r * Math.cos(rad(s.a));
        const y = HY + r * Math.sin(rad(s.a));
        return (
          <g key={i}>
            {s.gold && <circle cx={x} cy={y} r="11" fill="#e8c46d" opacity=".25" filter="url(#blur6)" />}
            <circle cx={x} cy={y} r={s.gold ? 4.5 : 3} fill={s.gold ? '#e8c46d' : '#fff'} opacity={s.gold ? 1 : 0.8} />
          </g>
        );
      })}
      <rect width="1440" height="900" fill="url(#scrimL)" />
      <rect width="1440" height="900" filter="url(#grain)" opacity=".09" style={{ mixBlendMode: 'overlay' }} />
    </svg>
  );
}
