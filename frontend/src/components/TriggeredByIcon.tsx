import { CalendarClock, Hand } from 'lucide-react'
import type { TriggeredBy } from '../lib/types'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './ui/tooltip'

// Visual config per trigger source. Two distinct icon colors so the two
// values are immediately distinguishable in dense tables — not just a pair
// of grey icons that the user has to hover to tell apart.
const CONFIG: Record<TriggeredBy, { icon: typeof Hand; color: string; label: string }> = {
  manual: {
    icon: Hand,
    color: 'text-violet-600',
    label: 'Triggered manually',
  },
  scheduler: {
    icon: CalendarClock,
    color: 'text-sky-600',
    label: 'Triggered by scheduler',
  },
}

export interface TriggeredByIconProps {
  value: TriggeredBy
}

// Compact icon + tooltip used in run tables. Wraps its own TooltipProvider so
// callers don't need to add one — the dashboard and job-detail tables both
// render many of these and treating them as self-contained keeps usage simple.
export default function TriggeredByIcon({ value }: TriggeredByIconProps) {
  const { icon: Icon, color, label } = CONFIG[value]
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span data-trigger-by={value} aria-label={label} className={`inline-flex ${color}`}>
            <Icon className="h-4 w-4" aria-hidden="true" />
          </span>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
