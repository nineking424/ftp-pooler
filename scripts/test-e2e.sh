#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

NAMESPACE="ftp-pooler"
KAFKA_NAMESPACE="kafka"
KAFKA_BOOTSTRAP="kafka-broker-0.kafka-broker.kafka:9092"
TASK_TOPIC="ftp-tasks"
RESULT_TOPIC="ftp-results"
FAIL_TOPIC="ftp-failures"

echo_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

echo_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

echo_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Wait for pod to be ready
wait_for_pod() {
    local label=$1
    local timeout=${2:-120}
    echo_info "Waiting for pod with label $label to be ready..."
    kubectl wait --for=condition=ready pod -l $label -n $NAMESPACE --timeout=${timeout}s
}

# Create test files on FTP server
setup_test_files() {
    echo_info "Setting up test files on FTP server..."

    FTP_POD=$(kubectl get pod -n $NAMESPACE -l app=ftp-server -o jsonpath='{.items[0].metadata.name}')

    # Create test directory and files
    kubectl exec -n $NAMESPACE $FTP_POD -- mkdir -p /home/vsftpd/ftpuser/testdir
    kubectl exec -n $NAMESPACE $FTP_POD -- sh -c 'echo "Test file content 1" > /home/vsftpd/ftpuser/test1.txt'
    kubectl exec -n $NAMESPACE $FTP_POD -- sh -c 'echo "Test file content 2" > /home/vsftpd/ftpuser/test2.txt'
    kubectl exec -n $NAMESPACE $FTP_POD -- sh -c 'dd if=/dev/urandom of=/home/vsftpd/ftpuser/large_file.bin bs=1024 count=1024 2>/dev/null'
    kubectl exec -n $NAMESPACE $FTP_POD -- chmod -R 755 /home/vsftpd/ftpuser

    echo_info "Test files created successfully"
}

# Send task to Kafka
send_task() {
    local task_json=$1
    echo_info "Sending task to Kafka: $task_json"

    # Use kafka-console-producer from Kafka namespace
    echo "$task_json" | kubectl exec -i -n $KAFKA_NAMESPACE kafka-broker-0 -- \
        kafka-console-producer.sh --broker-list $KAFKA_BOOTSTRAP --topic $TASK_TOPIC
}

# Read result from Kafka
read_result() {
    local topic=$1
    local timeout=${2:-30}
    echo_info "Reading from topic $topic (timeout: ${timeout}s)..."

    kubectl exec -n $KAFKA_NAMESPACE kafka-broker-0 -- \
        timeout $timeout kafka-console-consumer.sh \
        --bootstrap-server $KAFKA_BOOTSTRAP \
        --topic $topic \
        --from-beginning \
        --max-messages 1 2>/dev/null || true
}

# Test 1: Download from FTP to local
test_download() {
    echo ""
    echo_info "=========================================="
    echo_info "TEST 1: Download from FTP to local"
    echo_info "=========================================="

    local task_id="test-download-$(date +%s)"
    local task='{
        "task_id": "'$task_id'",
        "src_id": "remote-ftp",
        "src_path": "/test1.txt",
        "dst_id": "local",
        "dst_path": "/data/storage/downloaded_test1.txt"
    }'

    send_task "$task"
    sleep 5

    # Check result
    local result=$(read_result $RESULT_TOPIC 30)
    if echo "$result" | grep -q "$task_id"; then
        echo_info "Download test PASSED"
        return 0
    else
        echo_error "Download test FAILED"
        return 1
    fi
}

# Test 2: Upload from local to FTP
test_upload() {
    echo ""
    echo_info "=========================================="
    echo_info "TEST 2: Upload from local to FTP"
    echo_info "=========================================="

    # First, create a local file in the ftp-pooler pod
    POOLER_POD=$(kubectl get pod -n $NAMESPACE -l app=ftp-pooler -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n $NAMESPACE $POOLER_POD -- sh -c 'echo "Upload test content" > /data/storage/upload_test.txt'

    local task_id="test-upload-$(date +%s)"
    local task='{
        "task_id": "'$task_id'",
        "src_id": "local",
        "src_path": "/data/storage/upload_test.txt",
        "dst_id": "remote-ftp",
        "dst_path": "/uploaded_test.txt"
    }'

    send_task "$task"
    sleep 5

    # Check result
    local result=$(read_result $RESULT_TOPIC 30)
    if echo "$result" | grep -q "$task_id"; then
        echo_info "Upload test PASSED"
        return 0
    else
        echo_error "Upload test FAILED"
        return 1
    fi
}

# Test 3: Large file transfer
test_large_file() {
    echo ""
    echo_info "=========================================="
    echo_info "TEST 3: Large file download (1MB)"
    echo_info "=========================================="

    local task_id="test-large-$(date +%s)"
    local task='{
        "task_id": "'$task_id'",
        "src_id": "remote-ftp",
        "src_path": "/large_file.bin",
        "dst_id": "local",
        "dst_path": "/data/storage/large_file_downloaded.bin"
    }'

    send_task "$task"
    sleep 10

    # Check result
    local result=$(read_result $RESULT_TOPIC 60)
    if echo "$result" | grep -q "$task_id"; then
        echo_info "Large file test PASSED"
        return 0
    else
        echo_error "Large file test FAILED"
        return 1
    fi
}

# Test 4: Failure case - non-existent file
test_failure() {
    echo ""
    echo_info "=========================================="
    echo_info "TEST 4: Failure case - non-existent file"
    echo_info "=========================================="

    local task_id="test-failure-$(date +%s)"
    local task='{
        "task_id": "'$task_id'",
        "src_id": "remote-ftp",
        "src_path": "/nonexistent_file.txt",
        "dst_id": "local",
        "dst_path": "/data/storage/nonexistent.txt"
    }'

    send_task "$task"
    sleep 5

    # Check fail topic
    local result=$(read_result $FAIL_TOPIC 30)
    if echo "$result" | grep -q "$task_id"; then
        echo_info "Failure test PASSED (task correctly sent to fail topic)"
        return 0
    else
        echo_error "Failure test FAILED (task not found in fail topic)"
        return 1
    fi
}

# Check health endpoint
test_health() {
    echo ""
    echo_info "=========================================="
    echo_info "TEST: Health check endpoint"
    echo_info "=========================================="

    POOLER_POD=$(kubectl get pod -n $NAMESPACE -l app=ftp-pooler -o jsonpath='{.items[0].metadata.name}')
    local response=$(kubectl exec -n $NAMESPACE $POOLER_POD -- curl -s http://localhost:8080/health)

    if echo "$response" | grep -q "healthy"; then
        echo_info "Health check PASSED"
        return 0
    else
        echo_error "Health check FAILED: $response"
        return 1
    fi
}

# Check metrics endpoint
test_metrics() {
    echo ""
    echo_info "=========================================="
    echo_info "TEST: Prometheus metrics endpoint"
    echo_info "=========================================="

    POOLER_POD=$(kubectl get pod -n $NAMESPACE -l app=ftp-pooler -o jsonpath='{.items[0].metadata.name}')
    local response=$(kubectl exec -n $NAMESPACE $POOLER_POD -- curl -s http://localhost:9090/metrics)

    if echo "$response" | grep -q "ftp_pooler"; then
        echo_info "Metrics check PASSED"
        return 0
    else
        echo_error "Metrics check FAILED"
        return 1
    fi
}

# Main execution
main() {
    echo_info "Starting E2E tests for FTP Pooler"
    echo_info "Namespace: $NAMESPACE"
    echo ""

    local passed=0
    local failed=0

    # Wait for pods to be ready
    wait_for_pod "app=ftp-server"
    wait_for_pod "app=ftp-pooler"

    # Setup test files
    setup_test_files

    # Run tests
    if test_health; then ((passed++)); else ((failed++)); fi
    if test_metrics; then ((passed++)); else ((failed++)); fi
    if test_download; then ((passed++)); else ((failed++)); fi
    if test_upload; then ((passed++)); else ((failed++)); fi
    if test_large_file; then ((passed++)); else ((failed++)); fi
    if test_failure; then ((passed++)); else ((failed++)); fi

    # Summary
    echo ""
    echo_info "=========================================="
    echo_info "TEST SUMMARY"
    echo_info "=========================================="
    echo_info "Passed: $passed"
    echo_info "Failed: $failed"

    if [ $failed -gt 0 ]; then
        echo_error "Some tests failed!"
        exit 1
    else
        echo_info "All tests passed!"
        exit 0
    fi
}

# Parse arguments
case "${1:-run}" in
    setup)
        setup_test_files
        ;;
    download)
        test_download
        ;;
    upload)
        test_upload
        ;;
    large)
        test_large_file
        ;;
    failure)
        test_failure
        ;;
    health)
        test_health
        ;;
    metrics)
        test_metrics
        ;;
    run|*)
        main
        ;;
esac
