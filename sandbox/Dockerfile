# 검증 샌드박스 이미지. pytest만 미리 굽고, 프로젝트 의존성은
# 검증 1단계(네트워크 허용 컨테이너)에서 .deps 디렉토리에 설치한다.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest
