import { Briefcase, LayoutDashboard, PanelLeftClose, PanelLeftOpen, Settings } from 'lucide-react'
import type { ComponentType, SVGProps } from 'react'
import { NavLink } from 'react-router-dom'
import { cn } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  icon: ComponentType<SVGProps<SVGSVGElement>>
  /** True for routes whose children should also activate this item (e.g. /jobs/:id activates Jobs). */
  matchChildren?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/jobs', label: 'Jobs', icon: Briefcase, matchChildren: true },
  { to: '/settings', label: 'Settings', icon: Settings },
]

export interface SidebarProps {
  expanded: boolean
  onToggle: () => void
  /** Called when a nav link is clicked — used by Layout to close the mobile drawer. */
  onNavigate?: () => void
}

export default function Sidebar({ expanded, onToggle, onNavigate }: SidebarProps) {
  const ToggleIcon = expanded ? PanelLeftClose : PanelLeftOpen
  return (
    <div className="flex h-full flex-col">
      <div
        className={cn(
          'flex h-14 items-center border-b border-border px-3',
          expanded ? 'justify-between' : 'justify-center'
        )}
      >
        {expanded && (
          <span className="font-semibold tracking-tight text-foreground">Billa-Gates</span>
        )}
        <button
          type="button"
          onClick={onToggle}
          aria-label="Toggle navigation"
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <ToggleIcon className="h-5 w-5" />
        </button>
      </div>

      <nav aria-label="Primary" className="flex-1 space-y-1 p-2">
        {NAV_ITEMS.map((item) => (
          <NavItemLink key={item.to} item={item} expanded={expanded} onNavigate={onNavigate} />
        ))}
      </nav>
    </div>
  )
}

interface NavItemLinkProps {
  item: NavItem
  expanded: boolean
  onNavigate?: () => void
}

function NavItemLink({ item, expanded, onNavigate }: NavItemLinkProps) {
  const Icon = item.icon
  return (
    <NavLink
      to={item.to}
      end={!item.matchChildren}
      onClick={onNavigate}
      aria-label={expanded ? undefined : item.label}
      title={expanded ? undefined : item.label}
      className={({ isActive }) =>
        cn(
          'flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium transition-colors',
          expanded ? 'justify-start' : 'justify-center',
          isActive
            ? 'bg-primary/10 text-primary'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        )
      }
    >
      <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
      {expanded && <span>{item.label}</span>}
    </NavLink>
  )
}
