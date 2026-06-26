# 🛒 Texas Grocery MCP

[![PyPI version](https://badge.fury.io/py/texas-grocery-mcp.svg)](https://pypi.org/project/texas-grocery-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/mgwalkerjr95/texas-grocery-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mgwalkerjr95/texas-grocery-mcp/actions/workflows/ci.yml)

> 🤖 Let AI do your grocery shopping! An MCP server that connects Claude to H-E-B grocery stores.

**Search products, manage your cart, clip coupons, and more — all through natural conversation.**

⚠️ This project is **not affiliated with H-E-B**. It uses unofficial web APIs and browser automation against HEB.com; use responsibly and ensure your usage complies with applicable terms and laws.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🏪 **Store Search** | Find HEB stores by address or zip code |
| 🔍 **Product Search** | Search products with pricing and availability |
| 🛒 **Cart Management** | Add/remove items with human-in-the-loop confirmation |
| 📋 **Product Details** | Ingredients, nutrition facts, allergens, warnings |
| 🎟️ **Digital Coupons** | List, search, and clip coupons to save money |
| 🔐 **Chrome Session Sync** | Reuse your existing HEB login from Chrome — bypasses bot detection, no browser automation |

---

## 📦 Installation

### Quick Start (Recommended) 🚀

```bash
pip install texas-grocery-mcp
```

That's all you need for the recommended auth path: **sync your HEB session
straight from Google Chrome** (see [Session Management](#-session-management)).
No browser automation, no Playwright, no stored password.

**Prerequisites for Chrome sync:**
- Google Chrome installed, with a profile **logged into [heb.com](https://www.heb.com)**
- macOS or Linux (cookie decryption uses the OS keystore)
- On macOS: allow access to the **"Chrome Safe Storage"** Keychain entry when
  first prompted

### Optional: embedded-browser fallback

If you are *not* logged into HEB in Chrome and want the MCP to drive a login
itself, install the browser extra:

```bash
pip install texas-grocery-mcp[browser]
playwright install chromium
```

This enables the `session_refresh` fallback. Note: HEB's WAF blocks *headless*
browsers, so `session_refresh` only works in visible mode (`headless=False`)
and still requires you to complete the login by hand. **Chrome sync is faster
and more reliable** — prefer it.

---

## ⚙️ Configuration

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "heb": {
      "command": "uvx",
      "args": ["texas-grocery-mcp"],
      "env": {
        "HEB_DEFAULT_STORE": "590"
      }
    }
  }
}
```

> Set `HEB_DEFAULT_STORE` to your store ID so `product_search` returns prices
> without an explicit `store_id`. The `playwright` MCP server is only needed if
> you use the optional embedded-browser fallback.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HEB_DEFAULT_STORE` | Default store ID | None |
| `REDIS_URL` | Redis cache URL | None (in-memory) |
| `LOG_LEVEL` | Logging level | INFO |

---

## 🎯 Usage Examples

### 🏪 Finding a Store

```
User: Find HEB stores near Austin, TX

Agent uses: store_search(address="Austin, TX", radius_miles=10)
```

### 🔍 Searching Products

```
User: Search for organic milk

Agent uses: store_change(store_id="590")
Agent uses: product_search(query="organic milk")
```

### 📋 Getting Product Details

```
User: What are the ingredients in H-E-B olive oil?

Agent uses: product_search(query="heb olive oil")
Agent uses: product_get(product_id="127074")
# Returns: ingredients, nutrition facts, warnings, dietary attributes
```

The `product_get` tool returns:
- 🥗 **Ingredients** - Full ingredient statement
- 📊 **Nutrition Facts** - Complete FDA panel
- ⚠️ **Safety Warnings** - Allergen info and precautions
- 🌿 **Dietary Attributes** - Gluten-free, organic, vegan, kosher, etc.
- 📍 **Store Location** - Aisle or section

### 🛒 Adding to Cart

```
User: Add 2 gallons of milk to my cart

Agent uses: cart_add(product_id="123456", quantity=2)
# Returns preview for confirmation

Agent uses: cart_add(product_id="123456", quantity=2, confirm=true)
# ✅ Added to cart!
```

### 🎟️ Clipping Coupons

```
User: Find coupons for cereal

Agent uses: coupon_search(query="cereal")
Agent uses: coupon_clip(coupon_id="ABC123", confirm=true)
# ✅ Coupon clipped!
```

---

## 🔐 Session Management

HEB protects its site with Imperva's WAF (the `reese84` bot-detection token),
which **blocks headless browsers**. The reliable way to authenticate is to reuse
the session you already have in Chrome.

### ⭐ Chrome Session Sync (Recommended)

Log into [heb.com](https://www.heb.com) in Google Chrome once, then:

```
Agent uses: session_sync_from_chrome()
# Reads + decrypts your HEB cookies from Chrome and writes them to the auth file.
# ✅ session_status now shows authenticated: true
```

How it works: it reads Chrome's cookie database, decrypts it with the key from
your OS keystore (macOS Keychain / Linux Secret Service), and stores the HEB
cookies for the MCP. The synced `reese84` token is accepted by HEB's WAF when
replayed, so product, cart, and coupon tools work immediately — **no browser
window opens and no login flow runs.**

**Multiple Chrome profiles?** Call it once with no arguments to auto-detect the
profile that's logged into HEB. If the wrong account is picked, pass a profile
name (either the display name or the directory name):

```
Agent uses: session_sync_from_chrome(profile="Profile 1")
# or:        session_sync_from_chrome(profile="Your Profile Display Name")
```

The result's `profiles` field lists the available profiles if you're unsure.

**macOS note:** the first sync may trigger a one-time Keychain prompt to allow
reading "Chrome Safe Storage" — click **Allow**.

**Re-syncing:** cookies last a long time (the `reese84` token is good for weeks),
but if `session_status` ever reports `needs_refresh: true`, just run
`session_sync_from_chrome()` again.

### 🧭 Fallback: embedded browser

If you can't use Chrome sync, and you installed the `[browser]` extra:

```
Agent uses: session_refresh(headless=False)
# Opens a visible browser; complete the HEB login by hand, then tell the agent "done".
```

Headless mode (`session_refresh()`) is blocked by HEB's WAF and will return an
HTTP 401 — use `headless=False` or, better, Chrome sync.

---

## 🧰 Available Tools

### 🏪 Store Tools
| Tool | Description |
|------|-------------|
| `store_search` | Find stores by address |
| `store_change` | Set preferred store |
| `store_get_default` | Get current default store |

### 🔍 Product Tools
| Tool | Description |
|------|-------------|
| `product_search` | Search products with pricing |
| `product_search_batch` | Search multiple products (up to 20) |
| `product_get` | Get detailed product info |

### 🛒 Cart Tools
| Tool | Description |
|------|-------------|
| `cart_check_auth` | Check authentication status |
| `cart_get` | View cart contents |
| `cart_add` | Add item (requires confirmation) |
| `cart_add_many` | Bulk add multiple items |
| `cart_remove` | Remove item |

### 🎟️ Coupon Tools
| Tool | Description |
|------|-------------|
| `coupon_list` | List available coupons |
| `coupon_search` | Search coupons by keyword |
| `coupon_clip` | Clip a coupon |
| `coupon_clipped` | List your clipped coupons |

### 🔐 Session Tools
| Tool | Description |
|------|-------------|
| `session_sync_from_chrome` | **Sync your HEB login from Chrome (recommended)** |
| `session_status` | Check session health |
| `session_refresh` | Refresh/login via embedded browser (fallback) |
| `session_save_credentials` | Save credentials for auto-login (fallback) |
| `session_clear` | Logout |

---

## 📚 Documentation

- 🔧 [Troubleshooting Guide](docs/TROUBLESHOOTING.md) - Solutions for common issues
- 🤝 [Contributing](CONTRIBUTING.md) - How to contribute
- 📝 [Changelog](CHANGELOG.md) - Version history
- 🔒 [Security](SECURITY.md) - Security policy

---

## 🛠️ Development

```bash
# Clone repository
git clone https://github.com/mgwalkerjr95/texas-grocery-mcp
cd texas-grocery-mcp

# Install with dev dependencies
pip install -e ".[dev]"
playwright install chromium

# Run tests
pytest tests/ -v

# Linting & type checking
ruff check src/
mypy src/
```

### 🐳 Docker

```bash
docker-compose up --build
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    User's MCP Environment                    │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │  🔐 Google Chrome   │    │   🛒 Texas Grocery MCP      │ │
│  │  (your HEB login)   │    │   (Grocery Logic)           │ │
│  │   Cookies (SQLite)  │───▶│   session_sync_from_chrome  │ │
│  └─────────────────────┘    └─────────────────────────────┘ │
│        decrypt via OS keystore         │                     │
└────────────────────────────────────────┼─────────────────────┘
                                         │ cookies replayed via httpx
                                         ▼
                              🌐 HEB GraphQL / SSR API
```

---

## 📄 License

MIT © Michael Walker

---

<p align="center">
  Made with ❤️ in Texas 🤠
</p>
