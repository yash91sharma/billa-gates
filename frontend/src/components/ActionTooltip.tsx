import * as React from 'react'
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip'

export interface ActionTooltipProps {
  /** Plain-language explanation of what the action does and what it changes. */
  content: React.ReactNode
  /**
   * Set when the wrapped control is disabled. A disabled button receives no
   * pointer or focus events at all, so the wrapper has to become the tab stop
   * for the explanation to be reachable — and explaining *why* an action is
   * unavailable is exactly when the tooltip matters most.
   */
  disabled?: boolean
  side?: React.ComponentProps<typeof TooltipContent>['side']
  children: React.ReactNode
}

// Hover/focus explanation for an action button. The trigger is always a
// wrapping span rather than the button itself (`asChild` onto the button):
// browsers suppress events on disabled buttons, and pointer/focus events from
// an enabled button still bubble up to the span, so one code path covers both.
//
// Callers must be inside a TooltipProvider.
export default function ActionTooltip({
  content,
  disabled = false,
  side = 'bottom',
  children,
}: ActionTooltipProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="action-tooltip-trigger"
          className="inline-flex"
          // Only when disabled — an enabled button is its own tab stop, and a
          // second one would make tabbing through the header row twice as long.
          tabIndex={disabled ? 0 : undefined}
        >
          {children}
        </span>
      </TooltipTrigger>
      <TooltipContent side={side} className="max-w-xs text-left">
        {content}
      </TooltipContent>
    </Tooltip>
  )
}
