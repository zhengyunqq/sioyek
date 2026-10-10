// Sioyek OCR helper (Apple Vision).
//
// Protocol:
//   stdin : one job per line, "<page_index>\t<absolute_image_path>"
//   stdout: one JSON object per finished page (order not guaranteed):
//     {"page": 3, "lines": [{"text": "...", "box": [x, y, w, h],
//                            "tokens": [{"t": "...", "box": [x, y, w, h]}, ...]}, ...]}
//   All boxes are normalized to [0, 1] with the origin at the TOP-LEFT of the image.
//
// Build: swiftc -O ocr_vision.swift -o sioyek_ocr_vision

import Foundation
import Vision

let maxConcurrent = max(2, min(6, ProcessInfo.processInfo.activeProcessorCount / 2))
let semaphore = DispatchSemaphore(value: maxConcurrent)
let group = DispatchGroup()
let outputQueue = DispatchQueue(label: "sioyek.ocr.output")
let workQueue = DispatchQueue(label: "sioyek.ocr.work", attributes: .concurrent)

func isCJK(_ scalar: Unicode.Scalar) -> Bool {
    switch scalar.value {
    case 0x1100...0x11FF,   // Hangul Jamo
         0x2E80...0x2FDF,   // CJK radicals
         0x3000...0x30FF,   // CJK punctuation, Hiragana, Katakana
         0x3100...0x31FF,   // Bopomofo, Hangul compat, Katakana ext
         0x3400...0x4DBF,   // CJK Ext A
         0x4E00...0x9FFF,   // CJK Unified
         0xAC00...0xD7AF,   // Hangul syllables
         0xF900...0xFAFF,   // CJK compat ideographs
         0xFE30...0xFE4F,   // CJK compat forms
         0xFF00...0xFFEF:   // Full-width forms
        return true
    default:
        return false
    }
}

/// Splits a recognized line into tokens: whitespace separates tokens, and
/// boundaries between CJK and non-CJK runs also start a new token, so that
/// mixed text like "根据Attention" gets accurate per-script boxes.
/// `spaceAfter` tells whether the token is followed by whitespace in the line.
func tokenRanges(_ text: String) -> [(range: Range<String.Index>, spaceAfter: Bool)] {
    var ranges: [(range: Range<String.Index>, spaceAfter: Bool)] = []
    var start: String.Index? = nil
    var prevCJK = false
    var idx = text.startIndex
    while idx < text.endIndex {
        let ch = text[idx]
        let next = text.index(after: idx)
        if ch.isWhitespace {
            if let s = start { ranges.append((s..<idx, true)); start = nil }
        } else {
            let cjk = ch.unicodeScalars.first.map(isCJK) ?? false
            if let s = start, cjk != prevCJK {
                ranges.append((s..<idx, false))
                start = idx
            } else if start == nil {
                start = idx
            }
            prevCJK = cjk
        }
        idx = next
    }
    if let s = start { ranges.append((s..<text.endIndex, false)) }
    return ranges
}

func topLeftBox(_ r: CGRect) -> [Double] {
    let x = Double(r.origin.x), y = Double(r.origin.y)
    let w = Double(r.size.width), h = Double(r.size.height)
    return [x, 1.0 - y - h, w, h].map { ($0 * 1_000_000).rounded() / 1_000_000 }
}

func recognize(page: Int, path: String) -> [String: Any] {
    let url = URL(fileURLWithPath: path)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    if #available(macOS 13.0, *) {
        request.automaticallyDetectsLanguage = true
    }
    // Language hints (order = priority). Automatic detection still decides per page,
    // these simply make sure CJK models are available for mixed academic text.
    if let supported = try? request.supportedRecognitionLanguages() {
        let preferred = ["zh-Hans", "zh-Hant", "en-US", "ja-JP", "ko-KR"]
        let langs = preferred.filter { supported.contains($0) }
        if !langs.isEmpty { request.recognitionLanguages = langs }
    }

    let handler = VNImageRequestHandler(url: url, options: [:])
    var lines: [[String: Any]] = []
    do {
        try handler.perform([request])
        for obs in request.results ?? [] {
            guard let cand = obs.topCandidates(1).first else { continue }
            let text = cand.string
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
            var tokens: [[String: Any]] = []
            for (range, spaceAfter) in tokenRanges(text) {
                let tokenText = String(text[range])
                var box = obs.boundingBox
                if let rectObs = try? cand.boundingBox(for: range) {
                    box = rectObs.boundingBox
                }
                tokens.append(["t": tokenText, "box": topLeftBox(box), "sp": spaceAfter])
            }
            lines.append([
                "text": text,
                "box": topLeftBox(obs.boundingBox),
                "conf": Double(cand.confidence),
                "tokens": tokens,
            ])
        }
        return ["page": page, "lines": lines]
    } catch {
        return ["page": page, "lines": [], "error": error.localizedDescription]
    }
}

func emit(_ obj: [String: Any]) {
    outputQueue.sync {
        if let data = try? JSONSerialization.data(withJSONObject: obj, options: []) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write("\n".data(using: .utf8)!)
        }
    }
}

while let line = readLine(strippingNewline: true) {
    let parts = line.split(separator: "\t", maxSplits: 1).map(String.init)
    guard parts.count == 2, let page = Int(parts[0]) else { continue }
    let path = parts[1]
    semaphore.wait()
    group.enter()
    workQueue.async {
        let result = autoreleasepool { recognize(page: page, path: path) }
        emit(result)
        semaphore.signal()
        group.leave()
    }
}
group.wait()
