import {
  Briefcase,
  ExternalLink,
  HardDrive,
  LayoutDashboard,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
} from 'lucide-react'
import type { ComponentType, SVGProps } from 'react'
import { Link, NavLink } from 'react-router-dom'
import billaLogo from '@/assets/billa.png'
import { cn } from '@/lib/utils'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './ui/tooltip'

interface NavItem {
  to: string
  label: string
  icon: ComponentType<SVGProps<SVGSVGElement>>
  /** True for routes whose children should also activate this item (e.g. /jobs/:id activates Jobs). */
  matchChildren?: boolean
}

// The issue form, not the issue list: the link exists so a user who has just
// hit a bug lands on an empty report rather than on someone else's. The version
// is prefilled into the body because "which build?" is otherwise the first
// reply on every report, and this footer is the only place the number appears.
const ISSUE_URL = `https://github.com/yash91sharma/billa-gates/issues/new?body=${encodeURIComponent(
  `\n\n---\nBilla-Gates v${__APP_VERSION__}`
)}`

const ISSUE_LABEL = 'Report an issue'
const ISSUE_A11Y_LABEL = `${ISSUE_LABEL} on GitHub (opens in a new tab)`

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/jobs', label: 'Jobs', icon: Briefcase, matchChildren: true },
  { to: '/destinations', label: 'Destinations', icon: HardDrive },
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
    <TooltipProvider delayDuration={200}>
      <div className="flex h-full flex-col">
        <div
          className={cn(
            'flex h-14 items-center border-b border-border px-3',
            expanded ? 'justify-between' : 'justify-center'
          )}
        >
          {expanded && (
            <Link to="/" onClick={onNavigate} className="flex items-center gap-2">
              <img
                src={billaLogo}
                alt=""
                aria-hidden="true"
                className="h-6 w-6 shrink-0 [image-rendering:pixelated]"
              />
              <span className="font-heading font-semibold tracking-tight text-foreground">
                Billa-Gates
              </span>
            </Link>
          )}
          <button
            type="button"
            onClick={onToggle}
            aria-label="Toggle navigation"
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <ToggleIcon className="h-5 w-5" />
          </button>
        </div>

        <nav aria-label="Primary" className="flex-1 space-y-1 p-2">
          {NAV_ITEMS.map((item) => (
            <NavItemLink key={item.to} item={item} expanded={expanded} onNavigate={onNavigate} />
          ))}
        </nav>

        {/* Footer. Which build is running is the first thing you want when a
            backup misbehaves, and it is otherwise nowhere in the UI — so the
            report link sits directly above it, where the number it needs is
            already on screen. */}
        <div className="border-t border-border p-2">
          <IssueLink expanded={expanded} />
          <div
            className={cn(
              'px-3 pt-1 text-xs text-muted-foreground',
              expanded ? 'text-left' : 'px-0 text-center'
            )}
          >
            <span className="tabular-nums">
              {expanded ? `v${__APP_VERSION__}` : __APP_VERSION__}
            </span>
          </div>
        </div>
      </div>
    </TooltipProvider>
  )
}

/**
 * Footer link to the GitHub issue form.
 *
 * It is a plain `<a>`, not a `NavLink`, and deliberately does not call
 * `onNavigate`: the destination is another site in another tab, so closing the
 * mobile drawer behind it would leave the user back on a page they never asked
 * to return to.
 */
function IssueLink({ expanded }: { expanded: boolean }) {
  const link = (
    <a
      href={ISSUE_URL}
      target="_blank"
      // noopener is the security half (the new tab must not get `window.opener`
      // back into this app); noreferrer keeps the referrer off the request.
      rel="noopener noreferrer"
      aria-label={expanded ? undefined : ISSUE_A11Y_LABEL}
      className={cn(
        'flex h-10 items-center gap-3 rounded-md px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground',
        expanded ? 'justify-start' : 'justify-center'
      )}
    >
      <GitHubIcon className="h-5 w-5 shrink-0" />
      {expanded && (
        <>
          <span>{ISSUE_LABEL}</span>
          {/* The icon is the sighted affordance for "leaves this app"; the
              sentence is the same promise for a screen reader. */}
          <span className="sr-only">on GitHub (opens in a new tab)</span>
          <ExternalLink className="ml-auto h-3.5 w-3.5 shrink-0 opacity-60" aria-hidden="true" />
        </>
      )}
    </a>
  )

  if (expanded) {
    return link
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{ISSUE_LABEL}</TooltipContent>
    </Tooltip>
  )
}

/**
 * GitHub's mark. lucide dropped its brand icons in v1, so this is inlined
 * rather than imported — a generic bug or link glyph would not tell the user
 * where the button takes them, which is the whole point of a brand mark.
 */
function GitHubIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false" {...props}>
      <path d="M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.21 11.39.6.11.82-.26.82-.58 0-.29-.01-1.04-.01-2.05-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.84 2.81 1.31 3.5 1 .11-.78.42-1.31.76-1.61-2.67-.3-5.47-1.34-5.47-5.95 0-1.31.47-2.39 1.24-3.23-.12-.31-.54-1.53.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6.01 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.65.24 2.87.12 3.18.77.84 1.24 1.92 1.24 3.23 0 4.62-2.81 5.64-5.49 5.94.43.37.82 1.1.82 2.22 0 1.6-.02 2.89-.02 3.29 0 .32.22.7.83.58A12.01 12.01 0 0 0 24 12.5C24 5.87 18.63.5 12 .5Z" />
    </svg>
  )
}

interface NavItemLinkProps {
  item: NavItem
  expanded: boolean
  onNavigate?: () => void
}

function NavItemLink({ item, expanded, onNavigate }: NavItemLinkProps) {
  const Icon = item.icon
  const link = (
    <NavLink
      to={item.to}
      end={!item.matchChildren}
      onClick={onNavigate}
      aria-label={expanded ? undefined : item.label}
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

  if (expanded) {
    return link
  }

  // Collapsed rail: the label is the only thing identifying an icon, and the
  // native `title=` this used to rely on appears about a second late, in the
  // browser's own styling, disagreeing with every other tooltip in the app.
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  )
}
