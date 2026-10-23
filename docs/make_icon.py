"""Solo's app icon.

Lesson from v1: Slack already crops the icon to a rounded square, so artwork that draws its own
rounded frame — and then a speech bubble inside that — nests three containers before the subject
even starts. At the ~20px Slack actually renders, the subject disappears. So: full bleed, one
subject, big. The dice are the Han Solo reference; the app's name carries the rest.
"""
import random

V = 512
NAVY_TOP, NAVY_BOT = "#22365A", "#0A111E"
GOLD, GOLD_SHADE, GOLD_EDGE, PIP = "#F2B13C", "#C9801E", "#8A5512", "#1B1206"

random.seed(7)
stars = "".join(
    f'<circle cx="{random.uniform(0.03,0.97)*V:.1f}" cy="{random.uniform(0.03,0.97)*V:.1f}" '
    f'r="{random.uniform(0.004,0.011)*V:.2f}" fill="#fff" opacity="{random.randint(60,210)/255:.2f}"/>'
    for _ in range(55))

THREE = [(0.27, 0.27), (0.50, 0.50), (0.73, 0.73)]
TWO = [(0.30, 0.70), (0.70, 0.30)]


def die(i, cx, cy, size, angle, pips):
    h, r, pr = size / 2, size * 0.22, size * 0.097
    dots = "".join(
        f'<circle cx="{(fx-0.5)*size:.2f}" cy="{(fy-0.5)*size:.2f}" r="{pr:.2f}" fill="{PIP}"/>'
        for fx, fy in pips)
    return f'''  <g transform="translate({cx:.1f} {cy:.1f}) rotate({angle})">
    <clipPath id="c{i}"><rect x="{-h:.1f}" y="{-h:.1f}" width="{size:.1f}" height="{size:.1f}" rx="{r:.1f}"/></clipPath>
    <g clip-path="url(#c{i})">
      <rect x="{-h:.1f}" y="{-h:.1f}" width="{size:.1f}" height="{size:.1f}" fill="{GOLD}"/>
      <rect x="{-h:.1f}" y="{h-size*0.22:.1f}" width="{size:.1f}" height="{size*0.22:.1f}" fill="{GOLD_SHADE}"/>
    </g>
    <rect x="{-h:.1f}" y="{-h:.1f}" width="{size:.1f}" height="{size:.1f}" rx="{r:.1f}"
          fill="none" stroke="{GOLD_EDGE}" stroke-width="{size*0.055:.1f}"/>
{dots}
  </g>'''


svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {V} {V}" width="{V}" height="{V}">
  <title>Solo</title>
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0.3" y2="1">
      <stop offset="0" stop-color="{NAVY_TOP}"/><stop offset="1" stop-color="{NAVY_BOT}"/>
    </linearGradient>
  </defs>
  <rect width="{V}" height="{V}" fill="url(#sky)"/>
  {stars}
{die(1, 196, 214, 232, -14, THREE)}
{die(2, 330, 316, 196, 11, TWO)}
</svg>
'''
open(__import__("pathlib").Path(__file__).with_name("icon.svg"), "w").write(svg)
print("wrote docs/icon.svg")
