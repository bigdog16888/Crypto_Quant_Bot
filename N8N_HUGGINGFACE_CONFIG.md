# N8N + HuggingFace Configuration

## Authentication
### Hugging Face Access Token (Write Access)
- **Token**: `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`
- **Permissions**: Write (Repository/Space management)
- **Added**: 2026-01-26

### N8N API Token (CURRENT - 2026-01-26)
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI4MWU3MTFmZC05NmZiLTQxOGEtODQwMy1hODkzMTM1OGE0ZGQiLCJpc3MiOiJuOG4iLCJhdWQiOiJwdWJsaWMtYXBpIiwiaWF0IjoxNzY5NDA0MzM5fQ.Fjl9oWcCgT8alK_vQK1k8ceQxO4aOGy0zvwVcGgv8Hg
```

### GitHub Personal Access Token
```
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
```
- Permissions: `repo` (read private repos)
- Used for: Accessing `bigdog16888/n8n-backup`

## Instance Info
- **Space URL**: https://gionie-n8n-free.hf.space/
- **Repo ID**: `gionie/n8n-free` (Inferred)
- **API Base**: https://gionie-n8n-free.hf.space/api/v1

## Troubleshooting
### "Database is not ready" (SQLite Lock)
1. **Immediate Fix**: Restart Space
   ```bash
   curl -X POST -H "Authorization: Bearer $HF_TOKEN" https://huggingface.co/api/spaces/gionie/n8n-free/restart
   ```
2. **Permanent Fix**: Migrate to Postgres (Supabase/Neon)

## Current Status (2026-01-26 11:53 AM)
### Issue
n8n keeps getting "Database is not ready" (SQLite locks on HF free tier)

### What I Did
1. ✅ Restarted Space via API
2. ✅ Uploaded clean Dockerfile (removed ARG collisions)
3. ✅ Set Postgres config as **Variables** (not Secrets):
   - DB_TYPE=postgresdb
   - DB_POSTGRESDB_HOST=db.cspabbmlnwubeujyotlg.supabase.co
   - DB_POSTGRESDB_PORT=5432
   - DB_POSTGRESDB_DATABASE=postgres
   - DB_POSTGRESDB_USER=postgres
   - DB_POSTGRESDB_PASSWORD=7721431qQ@
4. ⏳ Space is BUILDING (may take 5-10 min)

### Next Steps When Online
1. Create new admin account (fresh DB)
2. Import workflows from `C:\Users\Gionie\Downloads\Backup.json`
3. Generate new API token in Settings > API

### If Still Broken
- Check build logs: https://huggingface.co/spaces/Gionie/n8n-free/logs
- May need to switch to different n8n Docker image or hosting

## History
- **2026-01-26**: Migrating to Supabase Postgres. Currently building.
- **2026-01-20**: Initial config created.
