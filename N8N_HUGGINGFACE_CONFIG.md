# N8N + HuggingFace Configuration

## Authentication Token (Created: 2026-01-20)
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJiOTY0ZmU0Zi0zZTU3LTQwNjAtOTJlMy1hOTRhMGJlMmI0ZjgiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwiaWF0IjoxNzY4ODk1MjU5LCJleHAiOjE3NzE0MzA0MDB9.Qr9LZvt6DZDq8D4R2NlXDCPFAbpJ2CXtQf6-HBYyUdo
```

## Instance URL
- **N8N URL**: https://gionie-n8n-free.hf.space/
- **API Base**: https://gionie-n8n-free.hf.space/api/v1

## Token Test Results
- **Status**: Returns 401 Unauthorized
- **Issue**: May need Authorization header with "Bearer " prefix
- **Action**: If 401 persists, generate NEW token from n8n UI

## How to Use
When calling API, include header:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9....
```

## Last Updated
2026-01-20
