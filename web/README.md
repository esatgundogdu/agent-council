# Looking at the UI

The screen was changed for a long time without anyone looking at it, which is how it
ended up with a comment that silently disabled a rule, prose set 1500px wide, and a
layout that overflowed its own window at 430px. These two scripts exist so that never
depends on someone remembering to check.

Neither installs anything: both drive the Chrome or Edge already on the machine over
the DevTools protocol.

```bash
node shot.mjs shots.example.json    # screenshots, one per entry, tabs and drawers included
node verify.mjs "<session url with ?token=>"
```

`shot.mjs` passes `--enable-unsafe-swiftshader`, so the WebGL room renders headless too
— on a software rasteriser, which is slower than the real thing and fine for a picture.

`verify.mjs` reports the two things a screenshot cannot: horizontal overflow at seven
widths from 1440 down to 375, and the contrast of every colour token against every
surface it is used on, in both themes. Both numbers should come back clean —

```
overflow px: 1440:-10 1100:-10 900:-10 700:-10 500:-10 430:-10 375:-10
contrast   : lightFail [] darkFail []  lightMin 4.76  darkMin 4.64
```

A session URL with a live token comes from `council up`.
