# FTP Pooler 아키텍처 문서

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [핵심 컴포넌트](#2-핵심-컴포넌트)
3. [데이터 흐름](#3-데이터-흐름)
4. [모듈 상세](#4-모듈-상세)
5. [설정 관리](#5-설정-관리)
6. [확장성](#6-확장성)
7. [에러 처리](#7-에러-처리)
8. [보안 고려사항](#8-보안-고려사항)

---

## 1. 시스템 개요

FTP Pooler는 Kafka 메시지 기반의 비동기 FTP 파일 전송 시스템입니다. 대량의 파일을 효율적으로 전송하기 위해 다음과 같은 설계 원칙을 따릅니다:

### 1.1 설계 원칙

| 원칙 | 설명 |
|------|------|
| **비동기 처리** | asyncio 기반으로 I/O 블로킹 없이 다수의 작업 동시 처리 |
| **연결 재사용** | FTP 세션 풀링으로 연결 오버헤드 최소화 |
| **메시지 기반** | Kafka를 통한 작업 분배로 느슨한 결합 유지 |
| **수평 확장** | StatefulSet으로 Pod 수 조절하여 처리량 확장 |
| **관찰 가능성** | 구조화된 로깅과 메트릭으로 시스템 상태 모니터링 |

### 1.2 시스템 아키텍처 다이어그램

```mermaid
flowchart TB
    Client[Client Application]

    subgraph Kafka[Kafka Cluster]
        ftp-tasks[ftp-tasks]
        ftp-results[ftp-results]
        ftp-failures[ftp-failures]
    end

    subgraph K8s[Kubernetes Cluster]
        subgraph StatefulSet[FTP Pooler StatefulSet]
            Pod0[Pod-0]
            Pod1[Pod-1]
        end

        subgraph SessionPool[FTP Session Pool]
            subgraph PoolA[Connection A Pool]
                SessA1[Sess 1]
                SessA2[Sess 2]
            end
            subgraph PoolB[Connection B Pool]
                SessB1[Sess 1]
                SessB2[Sess 2]
            end
        end

        subgraph FTPServers[FTP Servers]
            ServerA[A]
            ServerB[B]
            ServerC[C]
        end

        LocalStorage[Local Storage PVC\n/data/storage]
    end

    Client -->|Produce| ftp-tasks
    ftp-tasks -->|Consume| Pod0
    ftp-tasks -->|Consume| Pod1
    Pod0 --> SessionPool
    Pod1 --> SessionPool
    SessionPool -->|Produce| ftp-results
    SessionPool -->|Produce| ftp-failures
    SessionPool --> FTPServers
    SessionPool <--> LocalStorage
```

---

## 2. 핵심 컴포넌트

### 2.1 컴포넌트 구조

```mermaid
flowchart TB
    subgraph Application
        subgraph MainApp[Main Application]
            Lifecycle[생명주기 관리\ninitialize, start, stop]
            Signal[시그널 핸들링\nSIGTERM, SIGINT]
            Orchestration[컴포넌트 조율]
        end

        subgraph KafkaLayer[Kafka Layer]
            Consumer
            Producer
        end

        subgraph TransferEngine[Transfer Engine]
            PoolManager[Pool Manager]
        end

        subgraph APIServer[API Server]
            Routes
        end

        subgraph ConfigLayer[Config Layer]
            Settings[Settings\nconfig.yaml]
            ConnRegistry[Connection Registry\nconnections.ini]
        end
    end

    MainApp --> KafkaLayer
    MainApp --> TransferEngine
    MainApp --> APIServer

    Consumer --> PoolManager
    PoolManager --> Producer
```

### 2.2 컴포넌트 책임

| 컴포넌트 | 파일 | 책임 |
|----------|------|------|
| **Application** | `main.py` | 생명주기 관리, 시그널 핸들링, 컴포넌트 조율 |
| **TaskConsumer** | `kafka/consumer.py` | Kafka 메시지 소비, 역직렬화, 배치 처리 |
| **ResultProducer** | `kafka/producer.py` | 결과 메시지 발행, 토픽 라우팅 |
| **TransferEngine** | `transfer/engine.py` | 전송 방향 결정, 다운로드/업로드 실행 |
| **SessionPoolManager** | `pool/manager.py` | 연결별 세션 풀 관리, 세션 획득/반환 |
| **FTPSession** | `pool/session.py` | FTP 연결 래퍼, 파일 전송 메서드 |
| **ConnectionRegistry** | `config/connections.py` | FTP/로컬 연결 설정 저장소 |
| **Settings** | `config/settings.py` | 애플리케이션 설정 로딩 |
| **API Routes** | `api/routes.py` | REST API 엔드포인트 |

---

## 3. 데이터 흐름

### 3.1 작업 처리 흐름

```mermaid
flowchart TB
    subgraph Input[Task Input]
        KafkaConsumer[Kafka Consumer] --> TaskConsumer[Task Consumer]
        TaskConsumer --> TransferEngine[Transfer Engine]
    end

    subgraph Processing[Task Processing]
        TransferEngine --> DetermineDir[Determine Direction]
        TransferEngine --> AcquireSession[Acquire Session]
        TransferEngine --> ExecuteTransfer[Execute Transfer]

        AcquireSession --> SessionPool[Session Pool]
        AcquireSession --> CreateNew[Create New]

        SessionPool --> ReuseSession[Reuse Session]
        CreateNew --> ConnectFTP[Connect to FTP]

        ReuseSession --> Transfer[Download or Upload]
        ConnectFTP --> Transfer

        DetermineDir --> Download[Download\nremote→local]
        DetermineDir --> Upload[Upload\nlocal→remote]
    end

    subgraph Complete[Task Complete]
        Transfer --> ReleaseSession[Release Session]
        ExecuteTransfer --> CreateResult[Create Result]

        ReleaseSession --> PublishKafka[Publish to Kafka]
        CreateResult --> PublishKafka

        PublishKafka --> ftp-results[ftp-results\n성공]
        PublishKafka --> ftp-failures[ftp-failures\n실패]
    end
```

### 3.2 메시지 변환

```python
# 입력: TransferTask
{
    "task_id": "uuid-1234",
    "src_id": "remote-ftp",
    "src_path": "/data/file.txt",
    "dst_id": "local",
    "dst_path": "/storage/file.txt"
}

# 처리 후: TransferResult (성공)
{
    "task_id": "uuid-1234",
    "status": "success",
    "src_id": "remote-ftp",
    "src_path": "/data/file.txt",
    "dst_id": "local",
    "dst_path": "/storage/file.txt",
    "bytes_transferred": 1048576,
    "duration_ms": 1234,
    "timestamp": "2025-11-25T19:00:03.045406+00:00"
}

# 처리 후: TransferResult (실패)
{
    "task_id": "uuid-1234",
    "status": "failed",
    "error_code": "CONNECTION_ERROR",
    "error_message": "Connection refused",
    ...
}
```

---

## 4. 모듈 상세

### 4.1 FTP Session Pool

세션 풀은 FTP 연결을 효율적으로 관리합니다.

```mermaid
flowchart TB
    subgraph SessionPoolManager
        subgraph Settings
            S1[max_sessions_per_pod: 100]
            S2[max_sessions_per_connection: 10]
            S3[session_timeout_seconds: 300]
        end

        subgraph ConnectionPools[Connection Pools]
            subgraph PoolA[Pool: server-a\nmax: 10]
                A1[Session 1\nstate: IDLE]
                A2[Session 2\nstate: BUSY]
            end

            subgraph PoolB[Pool: server-b\nmax: 10]
                B1[Session 1\nstate: BUSY]
                B2[Session 2\nstate: IDLE]
            end

            PoolC[Pool: ...]
        end
    end
```

#### 세션 상태 전이

```mermaid
stateDiagram-v2
    [*] --> DISCONNECTED
    DISCONNECTED --> IDLE: connect()

    IDLE --> BUSY: acquire()
    BUSY --> IDLE: release()
    BUSY --> ERROR: error

    ERROR --> IDLE: recover()

    IDLE --> DISCONNECTED: disconnect()
```

### 4.2 Transfer Engine

전송 엔진은 작업의 방향을 결정하고 실행합니다.

```python
# 방향 결정 로직
def _determine_direction(task):
    src_is_local = registry.get(task.src_id).type == LOCAL
    dst_is_local = registry.get(task.dst_id).type == LOCAL

    if src_is_local and dst_is_local:
        raise ValueError("Both local")
    if not src_is_local and not dst_is_local:
        raise ValueError("Both remote")

    return UPLOAD if src_is_local else DOWNLOAD
```

```mermaid
flowchart TB
    subgraph TransferEngine
        subgraph DirectionDetection[Direction Detection]
            SrcID[src_id: remote-ftp] --> SrcType[type: FTP]
            DstID[dst_id: local] --> DstType[type: LOCAL]
            SrcType --> Direction[Direction: DOWNLOAD\nremote → local]
            DstType --> Direction
        end

        subgraph Download[_execute_download]
            D1[1. Resolve local path]
            D2[2. Acquire FTP session]
            D3[3. Download file]
            D4[4. Release session]
            D1 --> D2 --> D3 --> D4
        end

        subgraph Upload[_execute_upload]
            U1[1. Resolve local path]
            U2[2. Acquire FTP session]
            U3[3. Upload file]
            U4[4. Release session]
            U1 --> U2 --> U3 --> U4
        end
    end
```

### 4.3 Kafka Integration

```mermaid
flowchart TB
    subgraph KafkaIntegration[Kafka Integration]
        subgraph TaskConsumer
            TC_Config[bootstrap_servers: kafka:9092\ngroup_id: ftp-pooler\ntopic: ftp-tasks\nauto_offset_reset: earliest\nenable_auto_commit: false]

            subgraph MessageFlow[Message Flow]
                Kafka2[Kafka] --> Deserialize[Deserialize\nJSON]
                Deserialize --> Validate[Validate\nRequired fields]
                Validate --> TransferTask
            end
        end

        subgraph ResultProducer
            RP_Config[result_topic: ftp-results\nfail_topic: ftp-failures]

            subgraph RoutingLogic[Routing Logic]
                TransferResult --> SuccessCheck{status}
                SuccessCheck -->|SUCCESS| ftp-results2[ftp-results]
                SuccessCheck -->|FAILED| ftp-failures2[ftp-failures]
            end
        end
    end
```

---

## 5. 설정 관리

### 5.1 설정 계층

```mermaid
flowchart TB
    subgraph ConfigHierarchy[Configuration Hierarchy]
        L1[1. Environment Variables - 최우선\nFTP_POOLER_CONFIG=/etc/ftp-pooler/config.yaml\nFTP_POOLER_CONNECTIONS=/etc/ftp-pooler/connections.ini]

        L2[2. YAML Configuration File\nconfig.yaml: kafka, pool, api, metrics, logging]

        L3[3. Default Values - Pydantic\nSettings 클래스에 정의된 기본값]

        L1 --> L2 --> L3
    end
```

### 5.2 설정 클래스 구조

```python
Settings
├── kafka: KafkaSettings
│   ├── bootstrap_servers: list[str]  # ["localhost:9092"]
│   ├── consumer_group: str           # "ftp-pooler"
│   ├── input_topic: str              # "ftp-tasks"
│   ├── result_topic: str             # "ftp-results"
│   └── fail_topic: str               # "ftp-failures"
│
├── pool: PoolSettings
│   ├── max_sessions_per_pod: int          # 100
│   ├── max_sessions_per_connection: int   # 10
│   └── session_timeout_seconds: int       # 300
│
├── api: ApiSettings
│   ├── host: str   # "0.0.0.0"
│   └── port: int   # 8080
│
├── metrics: MetricsSettings
│   └── port: int   # 9090
│
├── logging: LoggingSettings
│   ├── level: str    # "INFO"
│   └── format: str   # "json"
│
├── config_path: Optional[str]
└── connections_path: Optional[str]
```

### 5.3 연결 레지스트리

```mermaid
flowchart TB
    subgraph ConnectionRegistry[Connection Registry]
        INI[connections.ini\n\nremote-server-a\ntype = ftp\nhost = ftp.example.com\nport = 21\nuser = username\npass = password\npassive = true]

        subgraph Registry[ConnectionRegistry]
            FTPConfig[FTPConnectionConfig\n\nconnection_id\ntype: FTP\nhost\nport\nuser\npassword\npassive]

            LocalConfig[LocalConnectionConfig\n\nconnection_id\ntype: LOCAL\nbase_path]
        end

        INI --> Registry
    end
```

---

## 6. 확장성

### 6.1 수평 확장 (Scale-Out)

```mermaid
flowchart TB
    subgraph HorizontalScaling[Horizontal Scaling]
        subgraph KafkaPartitions[Kafka Partitions: 3]
            P0[P0]
            P1[P1]
            P2[P2]
        end

        CG[Consumer Group: ftp-pooler]

        subgraph Pods[Pods]
            Pod0[Pod-0\nP0 담당]
            Pod1[Pod-1\nP1 담당]
            Pod2[Pod-2\nP2 담당]
        end

        P0 --> CG --> Pod0
        P1 --> CG --> Pod1
        P2 --> CG --> Pod2

        Scale[StatefulSet Scaling:\nkubectl scale statefulset ftp-pooler --replicas=3]
    end
```

### 6.2 연결별 동시성 제어

```mermaid
flowchart TB
    subgraph Pod0[Pod-0 - max_sessions_per_pod: 100]
        subgraph ServerAPool[Server-A Pool - max: 10]
            SA1[Session 1 - BUSY]
            SA2[Session 2 - IDLE]
            SA3[... 최대 10개]
        end

        subgraph ServerBPool[Server-B Pool - max: 10]
            SB1[Session 1 - BUSY]
            SB2[... 최대 10개]
        end

        Total[총 세션 수 ≤ 100]
    end
```

---

## 7. 에러 처리

### 7.1 에러 분류

| 에러 코드 | 분류 | 설명 | 재시도 가능 |
|-----------|------|------|-------------|
| INVALID_CONFIG | 설정 오류 | 소스/목적지 모두 로컬 또는 원격 | X |
| FILE_NOT_FOUND | 파일 오류 | 소스 파일 없음 | X |
| CONNECTION_ERROR | 연결 오류 | FTP 서버 연결 실패 | O |
| IO_ERROR | I/O 오류 | 파일 읽기/쓰기 실패 | O |
| UNKNOWN_ERROR | 기타 오류 | 예상치 못한 오류 | △ |

### 7.2 에러 처리 흐름

```mermaid
flowchart TB
    Execute[Execute Transfer]

    Execute --> Success
    Execute --> Error

    Success --> CreateSuccessResult[Create Result\nSUCCESS]
    Error --> CreateFailedResult[Create Result\nFAILED]

    CreateSuccessResult --> PublishResults[Publish\nftp-results]
    CreateFailedResult --> PublishFailures[Publish\nftp-failures]

    subgraph Logging[Logging - structlog]
        LogEntry["{\n  event: transfer_failed\n  task_id: uuid-1234\n  error_code: CONNECTION_ERROR\n  error: Connection refused\n  duration_ms: 5000\n}"]
    end

    Error --> Logging
```

---

## 8. 보안 고려사항

### 8.1 인증 정보 관리

```mermaid
flowchart TB
    subgraph CredentialManagement[Credential Management]
        subgraph K8sSecret[Kubernetes Secret]
            Secret["apiVersion: v1\nkind: Secret\nmetadata:\n  name: ftp-pooler-secrets\ndata:\n  connections.ini: base64 encoded"]
        end

        subgraph PodMount[Pod Volume Mount]
            Volumes["volumes:\n  - name: connections\n    secret:\n      secretName: ftp-pooler-secrets"]
            VolumeMounts["volumeMounts:\n  - name: connections\n    mountPath: /etc/ftp-pooler/connections.ini\n    subPath: connections.ini\n    readOnly: true"]
        end

        K8sSecret --> PodMount
    end
```

### 8.2 네트워크 보안

- FTP 세션은 패시브 모드 사용 권장
- Kubernetes NetworkPolicy로 Pod 간 통신 제한 가능
- FTP over TLS (FTPS) 지원 예정

### 8.3 컨테이너 보안

```dockerfile
# Non-root 사용자로 실행
RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password appuser

USER appuser

# Read-only 파일시스템 (쓰기는 PVC만)
# securityContext:
#   readOnlyRootFilesystem: true
```

---

## 부록

### A. 클래스 다이어그램

```mermaid
classDiagram
    Application --> Settings
    Application --> TaskConsumer
    Application --> TransferEngine
    Application --> ResultProducer

    TransferEngine --> SessionPoolManager
    SessionPoolManager --> SessionPool
    SessionPool --> FTPSession

    TaskConsumer --> TransferTask

    class Application {
        +Settings settings
        +TaskConsumer consumer
        +TransferEngine engine
        +ResultProducer producer
    }

    class Settings {
        +KafkaSettings kafka
        +PoolSettings pool
        +ApiSettings api
    }

    class SessionPoolManager {
        +manage()
    }

    class SessionPool {
        +acquire()
        +release()
    }

    class FTPSession {
        +connect()
        +disconnect()
        +download()
        +upload()
    }

    class TransferTask {
        +task_id
        +src_id
        +src_path
        +dst_id
        +dst_path
    }
```

### B. 시퀀스 다이어그램 (파일 다운로드)

```mermaid
sequenceDiagram
    participant Client
    participant Kafka
    participant Consumer
    participant Engine
    participant Pool
    participant FTP

    Client->>Kafka: produce()
    Kafka->>Consumer: consume()
    Consumer->>Engine: execute()
    Engine->>Pool: acquire()
    Pool-->>Engine: session
    Engine->>FTP: download()
    FTP-->>Engine: bytes
    Engine->>Pool: release()
    Engine-->>Consumer: result
    Consumer->>Kafka: produce()
```
