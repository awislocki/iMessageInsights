// Generates AppIcon.iconset (all macOS sizes) by drawing the icon with CoreGraphics.
// Run:  swift make-icon.swift        (then build.sh runs iconutil to make AppIcon.icns)
// Design: a Big Sur-style rounded-square in the app's blue, with a white speech
// bubble and a blue "insight" sparkle inside.

import AppKit

func starPath(cx: CGFloat, cy: CGFloat, outer: CGFloat, inner: CGFloat, points: Int) -> CGPath {
    let p = CGMutablePath()
    let n = points * 2
    for i in 0..<n {
        let radius = (i % 2 == 0) ? outer : inner
        let a = CGFloat.pi / 2 + CGFloat(i) * (2 * CGFloat.pi / CGFloat(n))
        let pt = CGPoint(x: cx + radius * cos(a), y: cy + radius * sin(a))
        if i == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
    }
    p.closeSubpath()
    return p
}

func drawIcon(_ s: CGFloat, _ ctx: CGContext) {
    let cs = CGColorSpaceCreateDeviceRGB()

    // --- rounded-square background (Big Sur grid: ~80.5% of canvas, 22.37% radius) ---
    let inset = s * 0.0977
    let ss = s - inset * 2
    let shape = CGRect(x: inset, y: inset, width: ss, height: ss)
    let radius = ss * 0.2237
    let bg = CGPath(roundedRect: shape, cornerWidth: radius, cornerHeight: radius, transform: nil)

    ctx.saveGState()
    ctx.addPath(bg); ctx.clip()
    let grad = CGGradient(colorsSpace: cs, colors: [
        CGColor(red: 0.33, green: 0.55, blue: 0.98, alpha: 1),   // top  #538cfa
        CGColor(red: 0.15, green: 0.39, blue: 0.92, alpha: 1)    // base #2663eb
    ] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(grad, start: CGPoint(x: 0, y: shape.maxY),
                           end: CGPoint(x: 0, y: shape.minY), options: [])
    ctx.restoreGState()

    // --- white speech bubble (body + tail) ---
    let bw = ss * 0.62, bh = ss * 0.50
    let bx = shape.minX + (ss - bw) / 2
    let by = shape.minY + ss * 0.30
    let br = bh * 0.30
    let body = CGPath(roundedRect: CGRect(x: bx, y: by, width: bw, height: bh),
                      cornerWidth: br, cornerHeight: br, transform: nil)
    let tail = CGMutablePath()
    tail.move(to: CGPoint(x: bx + bw * 0.20, y: by + 1))
    tail.addLine(to: CGPoint(x: bx + bw * 0.05, y: by - ss * 0.15))
    tail.addLine(to: CGPoint(x: bx + bw * 0.42, y: by + 1))
    tail.closeSubpath()

    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.addPath(body); ctx.fillPath()
    ctx.addPath(tail); ctx.fillPath()

    // --- blue "insight" sparkle inside the bubble ---
    let cx = bx + bw / 2, cy = by + bh * 0.54
    let star = starPath(cx: cx, cy: cy, outer: bh * 0.30, inner: bh * 0.30 * 0.34, points: 4)
    ctx.setFillColor(CGColor(red: 0.15, green: 0.39, blue: 0.92, alpha: 1))
    ctx.addPath(star); ctx.fillPath()
}

func writePNG(size: Int, to path: String) {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: size, height: size)
    NSGraphicsContext.saveGraphicsState()
    let g = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = g
    drawIcon(CGFloat(size), g.cgContext)
    NSGraphicsContext.restoreGraphicsState()
    let data = rep.representation(using: .png, properties: [:])!
    try! data.write(to: URL(fileURLWithPath: path))
}

let dir = "AppIcon.iconset"
try? FileManager.default.removeItem(atPath: dir)
try! FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

let specs: [(Int, String)] = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]
for (sz, name) in specs { writePNG(size: sz, to: "\(dir)/\(name)") }
print("wrote \(specs.count) PNGs to \(dir)")
