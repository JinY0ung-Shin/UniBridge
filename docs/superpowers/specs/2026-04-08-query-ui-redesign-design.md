# Query UI Full Redesign — Design Spec

## Context

API Hub Admin UI (`query-ui/`)는 내부 개발팀이 서비스 API DB를 관리하기 위한 도구다. 현재 디자인은 시스템 폰트, 라이트 테마, CSS 중복, 애니메이션 부재 등으로 generic한 인상을 준다. 전면 리뉴얼하여 모던 SaaS 대시보드 수준으로 끌어올린다.

## Design Decisions

| 항목 | 결정 |
|------|------|
| Theme | Vercel Dark — 순수 블랙(`#000`), 미니멀 보더, 블루 액센트 |
| Typography | Outfit (UI) + JetBrains Mono (code) via Google Fonts |
| Animation | 미니멀 — 호버/포커스 트랜지션(0.15s)만, 추가 라이브러리 없음 |
| CSS Architecture | CSS Custom Properties + 공통 `shared.css` 추출, 페이지별 CSS는 고유 레이아웃만 |
| Framework | 기존 React + vanilla CSS 유지, 새 의존성 추가 없음 |

## Design Tokens (CSS Custom Properties)

```css
:root {
  /* Background */
  --bg-root: #000000;
  --bg-primary: #0a0a0a;
  --bg-secondary: #111111;
  --bg-tertiary: #1a1a1a;
  --bg-hover: #1a1a1a;

  /* Border */
  --border-default: #222222;
  --border-subtle: #1a1a1a;
  --border-hover: #333333;

  /* Text */
  --text-primary: #ededed;
  --text-secondary: #a1a1a1;
  --text-tertiary: #666666;
  --text-inverse: #000000;

  /* Accent */
  --accent-blue: #0070f3;
  --accent-blue-hover: #0060d3;
  --accent-green: #50e3c2;
  --accent-red: #f31260;
  --accent-yellow: #f5a623;

  /* Radius */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 10px;

  /* Font */
  --font-sans: 'Outfit', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;

  /* Shadow */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.4);
  --shadow-md: 0 4px 12px rgba(0,0,0,0.5);
  --shadow-lg: 0 12px 40px rgba(0,0,0,0.6);
}
```

## Typography Scale

| Usage | Font | Weight | Size | Extra |
|-------|------|--------|------|-------|
| Page title | Outfit | 700 | 24px | letter-spacing: -0.5px |
| Section heading | Outfit | 600 | 16px | — |
| Body | Outfit | 400 | 13px | — |
| Table header | Outfit | 600 | 11px | uppercase, letter-spacing: 0.4px |
| Code / SQL | JetBrains Mono | 400 | 13px | — |
| Results table | JetBrains Mono | 400 | 12px | — |
| Badge | Outfit | 600 | 11px | — |
| Small label | Outfit | 600 | 12px | uppercase, letter-spacing: 0.3px |

## CSS Architecture

### File Structure (after)

```
src/
  styles/
    shared.css        # Design tokens + 공통 컴포넌트 (btn, table, badge, modal, form, states)
  index.css           # Reset + font import + body defaults (references shared.css tokens)
  components/
    Layout.css        # Sidebar, login overlay, main content shell
  pages/
    Dashboard.css     # Summary cards, DB grid (page-specific only)
    Connections.css    # (page-specific only, if any)
    Permissions.css    # Checkbox grid (page-specific only)
    AuditLogs.css     # Expandable rows, filter bar (page-specific only)
    QueryPlayground.css  # Editor container, results (page-specific only)
```

### shared.css Contains

공통 컴포넌트 스타일 (현재 5개 파일에 중복된 것들):
- `.btn`, `.btn-primary`, `.btn-secondary`, `.btn-danger`, `.btn-sm`
- `.table-container`, `.data-table` (thead, th, td, tr:hover)
- `.badge`, `.badge-ok`, `.badge-error`, `.badge-type`
- `.modal-overlay`, `.modal`, `.modal-header`, `.modal-close`, `.modal-actions`
- `.form-grid`, `.form-group`, `.form-group--full`, form inputs
- `.page-header`, `.page-header h1`, `.page-subtitle`, `.section-title`
- `.loading-message`, `.error-banner`, `.empty-state`
- `.status-dot`, `.status-dot--green`, `.status-dot--red`

## Component Design

### Sidebar
- Background: `--bg-root` (#000) — 콘텐츠와 동일, `border-right`로 분리
- Logo: 흰색 배경 정사각형(28px, radius 6px) + 검은 아이콘
- Nav links: 비활성 `--text-tertiary`, 활성 `--text-primary` + `--bg-secondary` 배경
- Footer: 버전 정보, `border-top`으로 분리

### Login Overlay
- Full screen `--bg-root` 배경
- 중앙 카드: `--bg-primary` + `--border-default` + `--shadow-lg`
- 폼 인풋: `--bg-secondary` 배경, 다크 스타일

### Buttons
- **Primary**: `--text-primary` 배경(흰), `--text-inverse` 텍스트(검정). hover → #fff
- **Secondary**: `--bg-secondary` 배경 + `--border-default` 보더. hover → border `--border-hover`
- **Danger**: transparent + `--accent-red` 텍스트 + red 30% 보더. hover → red 8% 배경

### Tables
- Container: `--bg-primary` + `--border-default` + `--radius-lg`
- Header: `--bg-secondary` 배경, `--text-tertiary` uppercase text
- Rows: `--border-subtle` 구분선, hover → `--bg-hover`
- Last row: border-bottom 없음

### Cards (Dashboard)
- `--bg-primary` + `--border-default` + `--radius-lg`
- hover → `border-color: --border-hover`
- Summary card values: 32px bold, 색상으로 상태 표현 (green/red)

### SQL Editor (Query Playground)
- 터미널 스타일 크롬: 상단 바에 "SQL" 라벨 + 3개 dots
- 에디터 영역: `--bg-primary` 배경, JetBrains Mono
- textarea 자체는 기존처럼 동작하되, 배경이 #0a0a0a로 변경

### Modals
- Overlay: `rgba(0,0,0,0.7)`
- Modal: `--bg-primary` + `--border-default` + radius 12px + `--shadow-lg`
- Form inputs: `--bg-secondary` 배경

### Error/Status States
- Error banner: `--accent-red` 10% 배경 + red 텍스트 + red 20% 보더
- Warning: `--accent-yellow` 12% 배경
- Empty state: centered, `--text-tertiary`
- Loading: centered text (추후 스켈레톤으로 업그레이드 가능)

## Pages

### Dashboard
- Summary cards (3열 grid): Total / Healthy / Failed
- DB connection grid (auto-fill, minmax 280px): 상태 dot + alias + type badge + pool stats
- Error 카드: red 보더 + 에러 메시지

### Connections
- 헤더에 "+ Add Connection" primary 버튼
- 테이블: Alias, Type, Host, Database, Status, Actions
- Actions: Edit(secondary) + Delete(danger) 버튼
- Add/Edit 모달: 2열 form grid

### Permissions
- 상단 role/database 추가 row
- 테이블: Role, Database, 각 operation별 checkbox 열
- Delete 버튼

### Audit Logs
- Filter bar: database select, status select, limit input
- 테이블: Timestamp, User, Database, SQL (truncated), Status
- 클릭 시 확장: 전체 SQL (dark code block), params, error, meta
- Pagination

### Query Playground
- DB selector + Execute 버튼 + shortcut hint
- SQL editor (터미널 크롬 + textarea)
- Results meta (rows + duration)
- Results table (모노스페이스, sticky header, NULL italic)

## What Does NOT Change

- React Router 구조 (5개 라우트)
- API client (`api/client.ts`)
- React Query 사용 패턴
- 컴포넌트 로직/state 관리
- Layout 컴포넌트 구조 (sidebar + main)
- 기능 범위 (CRUD, 쿼리 실행 등)

## Font Loading

`index.html`에 Google Fonts 링크 추가:
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

## Mockups

브라우저 목업 HTML 파일은 `.superpowers/brainstorm/` 디렉토리에 보존:
- `design-system.html` — 전체 디자인 시스템 (colors, typography, components)
- `layout-dashboard.html` — 사이드바 + 대시보드 전체 레이아웃
- `pages-connections.html` — Connections 테이블 + Add Connection 모달
- `pages-query.html` — Query Playground (에디터 + 결과 테이블)
