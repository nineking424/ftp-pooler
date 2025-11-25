# FTP Pooler

대량의 파일을 FTP 프로토콜을 통해 복사하는 고성능 분산 시스템

## 프로젝트 개요

- **처리량**: 하루 수백만 파일
- **파일 크기**: 수백 KB ~ 수 MB
- **전송 방향**: Upload 또는 Download (한쪽은 항상 로컬)
- **배포**: Kubernetes StatefulSet (scale-out)

## 기술 스택

- Python 3.11+
- asyncio + aiokafka + aioftp
- FastAPI (REST API)
- structlog (JSON structured logging)
- prometheus-client (메트릭)
- pytest + pytest-asyncio (테스트)

## 아키텍처

Kafka에서 작업 메시지를 consume하여 비동기로 FTP 전송을 수행하고, 결과를 Kafka 토픽에 publish합니다.

```
[Kafka 입력토픽] → [Consumer] → [asyncio + FTP Session Pool] → [Kafka 결과/실패 토픽]
```

## 프로젝트 구조

```
ftp-pooler/
├── src/ftp_pooler/       # 메인 소스코드
│   ├── main.py           # 진입점
│   ├── config/           # 설정 로더
│   ├── kafka/            # Kafka consumer/producer
│   ├── pool/             # FTP 세션 풀 관리
│   ├── transfer/         # 전송 엔진
│   ├── api/              # REST API (FastAPI)
│   └── metrics/          # Prometheus 메트릭
├── config/               # 설정 파일 예시
├── k8s/                  # Kubernetes 매니페스트
├── tests/                # 테스트 코드
├── requirements.txt      # 프로덕션 의존성
└── requirements-dev.txt  # 개발 의존성
```

## 주요 설정 파일

- `config/config.yaml`: 애플리케이션 설정 (Kafka, Pool, API, Metrics)
- `config/connections.ini`: FTP 접속 정보 (rclone 스타일)

## 코딩 컨벤션

- 비동기 함수는 `async def` 사용
- 타입 힌트 필수
- docstring은 Google 스타일
- 로깅은 structlog 사용 (JSON 포맷)
- 테스트 파일은 `test_*.py` 패턴

## 실행 명령어

```bash
# 의존성 설치
pip install -r requirements.txt

# 개발 의존성 설치
pip install -r requirements-dev.txt

# 테스트 실행
pytest

# 애플리케이션 실행
python -m ftp_pooler.main
```

## 참고 문서

- 상세 계획서: `.claude/plans/golden-snuggling-nebula.md`
