#!/usr/bin/env osascript -l JavaScript
// Draws the Polypress icon (1024x1024) with AppKit, nothing installed:
// a dark rounded square holding a pale page, and on the page four rows of a
// table that get shorter down the stack -- a table being pressed. One row is
// in the accent blue. Same square, page and accent as the other small apps
// on this Mac, so they read as one set.
//
//   osascript -l JavaScript app/make_icon.js out.png
ObjC.import('AppKit');

function run(argv) {
	const out = argv[0];
	const S = 1024;
	const img = $.NSImage.alloc.initWithSize($.NSMakeSize(S, S));
	img.lockFocus;

	const rect = (x, y, w, h, r) =>
		$.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius($.NSMakeRect(x, y, w, h), r, r);
	const rgb = (r, g, b) => $.NSColor.colorWithSRGBRedGreenBlueAlpha(r, g, b, 1.0);

	rgb(0.086, 0.086, 0.098).set;
	rect(0, 0, S, S, 224).fill;

	const px = 168, py = 168, pw = S - px * 2, ph = S - py * 2;
	rgb(0.973, 0.969, 0.957).set;
	rect(px, py, pw, ph, 64).fill;

	// four rows, top to bottom, each shorter than the last
	const widths = [1.0, 0.78, 0.56, 0.34];
	const bh = 70, gap = 46;
	const total = widths.length * bh + (widths.length - 1) * gap;
	const x0 = px + 110, full = pw - 220;
	let y = py + (ph + total) / 2 - bh;             // AppKit's y runs upward
	widths.forEach((w, i) => {
		if (i === 2) rgb(0.255, 0.333, 0.420).set;  // the accent
		else rgb(0.847, 0.839, 0.820).set;
		rect(x0, y, full * w, bh, bh / 2).fill;
		y -= bh + gap;
	});

	img.unlockFocus;
	const rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
	const png = rep.representationUsingTypeProperties(4 /* NSPNGFileType */, $.NSDictionary.dictionary);
	png.writeToFileAtomically($(out), true);
	return out;
}
