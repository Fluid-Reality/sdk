# Hand demo UI concepts, second pass

These five static 1280 × 800 previews explore different layouts and treatments
for the same five-finger actuator demo. They are design studies; the running
GUI in `../app.py` has not been changed.

1. **Studio** — a light product UI with a large hand and compact controls.
2. **Signal** — a dark live-output console with waveform emphasis.
3. **Handscape** — a warm, spacious interface with pattern choices and mapping
   controls below the hand.
4. **Blueprint** — a technical hand schematic with explicit channel assignment.
5. **Focus** — a minimal sidebar with a large hand and a selected-finger readout.

Run `python make_concepts.py` on Windows with Chrome installed to regenerate
the SVG and PNG previews.

The hand outline adapts ["Hand left.svg" by Cy21](https://commons.wikimedia.org/wiki/File:Hand_left.svg),
licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).
The outline has been filled, mirrored, recolored, and combined with fingertip
markers for these previews. Derived hand artwork in this folder is shared under
the same license.
