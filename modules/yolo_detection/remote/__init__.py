"""원격 YOLO — Mac 이 JPEG 를 HTTP 로 Orin 에 보내고 Frame 을 받는다 (SIM-05).

- protocol.py: Orin 서버와 Mac 클라이언트가 함께 쓰는 와이어 프로토콜 (stdlib 만)
- server.py:   Orin 쪽 YOLO HTTP 서버 (실행 시에만 yolo 그룹 필요)

여기서 아무것도 re-export 하지 않는다 — protocol 만 import 해도 server 가 딸려 오지 않게.
"""
