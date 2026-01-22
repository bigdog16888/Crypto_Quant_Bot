# OpenCode Context Memory & Crash Recovery Plugins

## Recommendation Summary

Two plugins recommended for context persistence and crash recovery:

1. **opencode-supermemory** - Persistent memory across sessions
2. **opencode-workflows** - SQLite-based crash recovery

---

## 📦 Plugin 1: opencode-supermemory

**GitHub**: https://github.com/supermemoryai/opencode-supermemory
**Stars**: 474
**Purpose**: Gives coding agents persistent memory using Supermemory

### What It Does
- Remembers context across sessions
- Learns from past conversations
- Vector-based memory retrieval for relevant context

### Installation

```bash
# Clone repository
git clone https://github.com/supermemoryai/opencode-supermemory.git
cd opencode-supermemory

# Install dependencies
npm install

# Build
npm run build
```

### package.json Details
```json
{
  "name": "opencode-supermemory",
  "version": "0.1.5",
  "type": "module",
  "bin": {
    "opencode-supermemory": "./dist/cli.js"
  },
  "dependencies": {
    "@opencode-ai/plugin": "^1.0.162",
    "supermemory": "^4.0.0"
  },
  "opencode": {
    "type": "plugin",
    "hooks": ["chat.message", "event"]
  }
}
```

---

## 📦 Plugin 2: opencode-workflows

**GitHub**: https://github.com/mark-hingston/opencode-workflows
**Stars**: 12
**Purpose**: Workflow automation plugin using Mastra engine with SQLite crash recovery

### What It Does
- SQLite-based workflow persistence
- Auto-save workflow state
- Resume after crashes
- Built-in workflow automation

### Installation

```bash
# Clone repository
git clone https://github.com/mark-hingston/opencode-workflows.git
cd opencode-workflows

# Install dependencies
npm install

# Build
npm run build
```

### package.json Details
```json
{
  "name": "opencode-workflows",
  "version": "0.7.0",
  "type": "module",
  "dependencies": {
    "@mastra/core": "^0.24.0",
    "@mastra/libsql": "^0.16.0",
    "@opencode-ai/plugin": "^1.0.147",
    "js-yaml": "^4.1.1",
    "node-cron": "^4.2.1",
    "zod": "^3.25.0"
  },
  "peerDependencies": {
    "isolated-vm": "^5.0.0",
    "opencode": "*"
  }
}
```

---

## ⚙️ Configuration

Add to your OpenCode config file (`~/.opencode/config.json` or `.opencode/` directory):

```json
{
  "plugins": {
    "opencode-supermemory": {
      "enabled": true,
      "apiKey": "your-supermemory-api-key",
      "storage": "local"
    },
    "opencode-workflows": {
      "enabled": true,
      "storage": "sqlite",
      "dbPath": "~/.opencode/workflows.db",
      "autoSaveInterval": 30000,
      "enableCrashRecovery": true
    }
  }
}
```

---

## 🚀 Usage After Installation

### Start OpenCode with Plugins
```bash
# If using OpenCode CLI
opencode start

# Plugins auto-load and start saving state
```

### After a Crash
```bash
# Restore previous session
opencode restore

# Or resume specific workflow
opencode resume --workflow <workflow-name>
```

### Manual Commands
```bash
# Save current state
opencode save

# View saved sessions
opencode sessions list

# Clear memory (if needed)
opencode memory clear
```

---

## 🔧 Prerequisites

| Requirement | Version |
|-------------|---------|
| Node.js | 18+ |
| npm or bun | Latest |
| SQLite | 3.x (usually pre-installed) |
| OpenCode CLI | Optional but recommended |

---

## 📂 Installation Comparison

| Feature | opencode-supermemory | opencode-workflows |
|---------|---------------------|-------------------|
| Memory persistence | ✓ | Partial |
| Crash recovery | ✗ | ✓ |
| SQLite storage | ✗ | ✓ |
| Workflow automation | ✗ | ✓ |
| Vector search | ✓ | ✗ |
| Cross-session learning | ✓ | ✗ |

---

## 🎯 Recommended Setup

For maximum crash resilience and memory persistence:

1. **Install both plugins**
2. **Configure auto-save** (30 second interval)
3. **Set up SQLite database path**
4. **Enable crash recovery**
5. **Test with a session**, crash it intentionally, then restore

---

## 📖 Related Resources

- **Supermemory Main**: https://github.com/supermemoryai
- **Supermemory Website**: https://supermemory.com
- **Mastra Engine**: https://github.com/mastra-ai/mastra
- **OpenCode Repo**: https://github.com/opencode-ai/opencode
- **OpenCode Documentation**: https://docs.opencode.ai

---

## 📝 Quick Install Commands (Copy-Paste)

```bash
# 1. Install opencode-supermemory
git clone https://github.com/supermemoryai/opencode-supermemory.git
cd opencode-supermemory
npm install
npm run build

# 2. Install opencode-workflows
cd ..
git clone https://github.com/mark-hingston/opencode-workflows.git
cd opencode-workflows
npm install
npm run build

# 3. Configure OpenCode
# Edit ~/.opencode/config.json with the config above
```

---

## ⚠️ Notes

- Both plugins require Node.js 18+
- Some features require OpenCode CLI to be installed
- Supermemory may require an API key from supermemory.com
- Test in a non-critical environment first
- Check plugin compatibility with your OpenCode version

---

**Last Updated**: 2026-01-20
**Status**: Ready for installation
