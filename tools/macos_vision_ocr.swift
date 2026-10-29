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
for path in CommandLine.arguments.dropFirst() {
    guard let image = cgImage(from: path) else {
        continue
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { observation in
            observation.topCandidates(1).first?.string
        }
        output.append(contentsOf: lines)
    } catch {
        continue
    }
}

print(output.joined(separator: "\n"))
