# FTP Pooler

대량의 파일을 FTP 프로토콜을 통해 복사하는 고성능 분산 시스템

## 특징

- **고성능**: asyncio 기반 비동기 처리로 동시에 다수의 파일 전송
- **확장성**: Kubernetes StatefulSet으로 손쉬운 scale-out
- **안정성**: FTP 세션 풀링으로 연결 재사용 및 관리
- **모니터링**: Prometheus 메트릭 및 JSON 구조화 로깅

## 아키텍처

```
[Kafka 입력토픽] → [Consumer] → [asyncio + FTP Session Pool] → [Kafka 결과/실패 토픽]
```

- Kafka에서 작업 메시지 consume
- 접속ID별 FTP 세션 풀 관리
- 비동기 파일 전송 (upload/download)
- 결과/실패 토픽으로 결과 publish

## 기술 스택

| 구분 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| 비동기 | asyncio, aiokafka, aioftp |
| API | FastAPI |
| 로깅 | structlog (JSON) |
| 모니터링 | Prometheus |
| 배포 | Kubernetes StatefulSet |

## 설치

```bash
# 의존성 설치
pip install -r requirements.txt

# 개발 의존성 설치 (테스트 포함)
pip install -r requirements-dev.txt
```

## 설정

### 접속 정보 (config/connections.ini)

```ini
[remote-server-a]
type = ftp
host = ftp.example.com
port = 21
user = username
pass = password
passive = true

[local]
type = local
base_path = /mnt/storage
```

### 애플리케이션 설정 (config/config.yaml)

```yaml
kafka:
  bootstrap_servers:
    - kafka-0:9092
  consumer_group: ftp-pooler
  input_topic: ftp-tasks
  result_topic: ftp-results
  fail_topic: ftp-failures

pool:
  max_sessions_per_pod: 100
  max_sessions_per_connection: 10
```

## 실행

```bash
python -m ftp_pooler.main
```

## 테스트

```bash
pytest
```

## 메시지 형식

### 입력 메시지

```json
{
  "task_id": "uuid",
  "src_id": "remote-server-a",
  "src_path": "/data/file.txt",
  "dst_id": "local",
  "dst_path": "/mnt/storage/file.txt"
}
```

### 결과 메시지

```json
{
  "task_id": "uuid",
  "status": "success",
  "bytes_transferred": 1048576,
  "duration_ms": 1234
}
```

## 라이선스

MIT
