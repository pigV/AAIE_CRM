from dataclasses import replace

from ..shared.recognition_text import normalize_for_comparison


def finalize_clean_segments(segments):
    ordered = tuple(sorted(
        segments,
        key=lambda item: (item.start_ms, item.end_ms, item.id),
    ))
    output = []
    last_included = None
    for item in ordered:
        if not item.included_in_clean_transcript or not item.clean_text:
            output.append(item)
            continue
        duplicate = bool(
            last_included is not None
            and item.start_ms <= last_included.end_ms
            and normalize_for_comparison(item.clean_text)
            == normalize_for_comparison(last_included.clean_text)
        )
        if duplicate:
            output.append(replace(
                item,
                included_in_clean_transcript=False,
                fusion_status="CROSS_MODAL_DUPLICATE_SUPPRESSED",
                selection_reason="overlapping_exact_normalized_duplicate",
            ))
            continue
        output.append(item)
        last_included = item
    output = tuple(output)
    clean_text = "\n".join(
        item.clean_text for item in output
        if item.included_in_clean_transcript and item.clean_text
    )
    return output, clean_text


def assemble_clean_raw_transcript(segments):
    return finalize_clean_segments(segments)[1]
