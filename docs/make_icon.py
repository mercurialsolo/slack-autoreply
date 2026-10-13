import random

V = 512
NAVY_TOP, NAVY_BOT = "#1F304A", "#0A111E"
CREAM, GOLD, GOLD_SHADE, PIP = "#F4EBD8", "#E6AA3A", "#C68A26", "#261E16"

random.seed(7)
stars = []
for _ in range(60):
    x, y = random.uniform(0.06, 0.94) * V, random.uniform(0.06, 0.94) * V
    r = random.uniform(0.0035, 0.010) * V
    a = random.randint(70, 215) / 255
    stars.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.2f}" fill="#fff" opacity="{a:.2f}"/>')

THREE = [(0.26, 0.26), (0.50, 0.50), (0.74, 0.74)]
TWO   = [(0.30, 0.70), (0.70, 0.30)]

def die(idx, cx, cy, size, angle, pips):
    h, r, pr = size / 2, size * 0.24, size * 0.088
    sw = max(1.0, size * 0.05)
    dots = "".join(
        f'<circle cx="{(fx - 0.5) * size:.2f}" cy="{(fy - 0.5) * size:.2f}" r="{pr:.2f}" fill="{PIP}"/>'
        for fx, fy in pips)
    return f'''  <g transform="translate({cx:.2f} {cy:.2f}) rotate({angle})">
    <clipPath id="dc{idx}"><rect x="{-h:.2f}" y="{-h:.2f}" width="{size:.2f}" height="{size:.2f}" rx="{r:.2f}"/></clipPath>
    <g clip-path="url(#dc{idx})">
      <rect x="{-h:.2f}" y="{-h:.2f}" width="{size:.2f}" height="{size:.2f}" rx="{r:.2f}" fill="{GOLD}"/>
      <rect x="{-h:.2f}" y="{h - size * 0.26:.2f}" width="{size:.2f}" height="{size * 0.26:.2f}" fill="{GOLD_SHADE}"/>
    </g>
    <rect x="{-h:.2f}" y="{-h:.2f}" width="{size:.2f}" height="{size:.2f}" rx="{r:.2f}"
          fill="none" stroke="{GOLD_SHADE}" stroke-width="{sw:.2f}"/>
{dots}
  </g>'''

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {V} {V}" width="{V}" height="{V}">
  <title>Solo</title>
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{NAVY_TOP}"/><stop offset="1" stop-color="{NAVY_BOT}"/>
    </linearGradient>
    <clipPath id="card"><rect width="{V}" height="{V}" rx="{V * 0.225:.1f}"/></clipPath>
  </defs>
  <g clip-path="url(#card)">
    <rect width="{V}" height="{V}" fill="url(#sky)"/>
{chr(10).join("    " + s for s in stars)}
  </g>
  <path d="M153.6 307.2 L153.6 404.5 L233.0 305.2 Z" fill="{CREAM}"/>
  <rect x="76.8" y="89.6" width="358.4" height="227.8" rx="53.8" fill="{CREAM}"/>
{die(1, 232.8, 207.2, 128.0, 16, THREE)}
{die(2, 345.0, 219.6, 110.1, -13, TWO)}
</svg>
'''
open("icon.svg", "w").write(svg)
print("wrote icon.svg")
