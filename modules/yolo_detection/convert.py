"""Ultralytics boxes -> Detection 변환 (DET-01).

boxes 에서는 `.tolist()` 만 쓴다 — numpy/torch 없이 돌고, 테스트는 리스트 가짜로 충분하다.
클래스 어휘는 `modules.yolo_detection.interface.detection.COCO_CLASS_IDS` 가 소유한다. `model.names` 는 COCO 80종 이름이
프로젝트의 4종 어휘 밖으로 새어 나가므로 일부러 쓰지 않는다.
"""

from typing import Any

from modules.yolo_detection.interface.detection import COCO_CLASS_IDS, Detection

COCO_NAME_BY_ID: dict[int, str] = {class_id: name for name, class_id in COCO_CLASS_IDS.items()}
TRACKED_CLASS_IDS: list[int] = sorted(COCO_NAME_BY_ID)  # model.track(classes=...) 로 YOLO 쪽에서 미리 거른다
MAX_CONFIDENCE = 1.0  # Detection.from_dict 가 [0, 1] 밖을 거부하므로 부동소수 오차를 자른다
XYXY_DECIMALS = 1  # mock 박스와 같은 자릿수


def convert_boxes(boxes: Any) -> tuple[Detection, ...]:
    if boxes is None:
        return ()

    class_id_list = boxes.cls.tolist()
    if not class_id_list:
        return ()

    xyxy_list = boxes.xyxy.tolist()
    confidence_list = boxes.conf.tolist()
    track_id_list = boxes.id.tolist() if boxes.id is not None else [None] * len(class_id_list)

    detections: list[Detection] = []
    for xyxy, class_id, confidence, track_id in zip(xyxy_list, class_id_list, confidence_list, track_id_list):
        class_id = int(class_id)
        if class_id not in COCO_NAME_BY_ID:
            continue
        detections.append(Detection(
            class_id=class_id,
            class_name=COCO_NAME_BY_ID[class_id],
            confidence=min(MAX_CONFIDENCE, float(confidence)),
            xyxy=tuple(round(float(v), XYXY_DECIMALS) for v in xyxy),
            track_id=None if track_id is None else int(track_id),
        ))
    return tuple(detections)
