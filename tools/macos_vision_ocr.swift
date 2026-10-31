import AppKit
import Foundation
import Vision

func cgImage(from path: String) -> CGImage? {
    guard let image = NSImage(contentsOfFile: path) else {
        return nil
    }
    var rect = NSRect(origin: .zero, size: image.size)
    return image.cgImage(forProposedRect: &rect, context: nil, hints: nil)
}

var output: [String] = []
var failed = false
for path in CommandLine.arguments.dropFirst() {
    guard let image = cgImage(from: path) else {
        failed = true
        continue
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
        // Vision may return the entire label column before the value column.
        // Reconstruct visual rows so Start date stays paired with its value.
        let observations = (request.results ?? []).sorted {
            $0.boundingBox.midY > $1.boundingBox.midY
        }
        var rows: [[VNRecognizedTextObservation]] = []
        for observation in observations {
            if let last = rows.last, let anchor = last.first,
               abs(anchor.boundingBox.midY - observation.boundingBox.midY) <
                min(anchor.boundingBox.height, observation.boundingBox.height) * 0.55 {
                rows[rows.count - 1].append(observation)
            } else {
                rows.append([observation])
            }
        }
        let lines = rows.map { row in
            row.sorted { $0.boundingBox.minX < $1.boundingBox.minX }
                .compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
        }
        output.append(contentsOf: lines)
    } catch {
        failed = true
        continue
    }
}

if failed {
    fputs("OCR recognition failed\n", stderr)
    exit(1)
}
print(output.joined(separator: "\n"))
