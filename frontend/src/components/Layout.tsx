import { Menu } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { cn } from '@/lib/utils'
import Sidebar from './Sidebar'

const MOBILE_BREAKPOINT_PX = 768

/**
 * App shell with a left sidebar and a content outlet.
 *
 * Two states drive the sidebar:
 *   - `expanded` (desktop): full-width with labels vs. icon-only rail.
 *   - `mobileOpen` (< md): drawer is hidden by default; the header hamburger
 *     toggles it. A backdrop closes it on tap.
 *
 * These are intentionally separate because the UX is different on each
 * breakpoint — collapsing the rail on desktop isn't the same action as
 * dismissing the drawer on mobile. One state per concern keeps the
 * transitions easy to reason about.
 */
export default function Layout() {
  const [expanded, setExpanded] = useState(true)
  const [mobileOpen, setMobileOpen] = useState(false)

  // Auto-close the mobile drawer when the viewport grows past the breakpoint,
  // so resizing from mobile → desktop doesn't leave a stale overlay open.
  useEffect(() => {
    function handleResize() {
      if (window.innerWidth >= MOBILE_BREAKPOINT_PX) {
        setMobileOpen(false)
      }
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  return (
    <div className="flex min-h-screen bg-background">
      {/* Mobile drawer backdrop */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          aria-hidden="true"
          onClick={() => setMobileOpen(false)}
        />
      )}

      <aside
        role="complementary"
        aria-label="Sidebar"
        data-expanded={expanded ? 'true' : 'false'}
        data-mobile-open={mobileOpen ? 'true' : 'false'}
        className={cn(
          'fixed inset-y-0 left-0 z-40 border-r border-border bg-sidebar text-sidebar-foreground transition-[width,transform] duration-200 ease-in-out',
          'md:relative md:translate-x-0',
          expanded ? 'w-60' : 'w-16',
          mobileOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'
        )}
      >
        <Sidebar
          expanded={expanded}
          onToggle={() => setExpanded((v) => !v)}
          onNavigate={() => setMobileOpen(false)}
        />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Mobile-only header with hamburger */}
        <header className="flex h-14 items-center gap-3 border-b border-border bg-card px-4 md:hidden">
          <button
            type="button"
            onClick={() => setMobileOpen(true)}
            aria-label="Open navigation"
            className="inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <Menu className="h-5 w-5" />
          </button>
          <span className="font-semibold tracking-tight">Billa-Gates</span>
        </header>

        <main className="min-w-0 flex-1 overflow-x-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
