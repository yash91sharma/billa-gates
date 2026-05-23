import { Info } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip'

export interface FieldHelp {
  label: string
  optional?: boolean
  description: string
  example?: string
}

export function helpId(htmlFor: string): string {
  return `${htmlFor}-help`
}

// Compose a label + (optional) chip + Radix tooltip trigger + a screen-reader
// description that is linked to the input via aria-describedby. The same text
// shown visually in the tooltip is also exposed to assistive tech, so the help
// is accessible without hovering.
//
// `variant="block"` (default) renders a stacked label above an input.
// `variant="inline"` is for checkbox rows where the label sits beside the box.
interface FieldLabelProps {
  htmlFor: string
  help: FieldHelp
  variant?: 'block' | 'inline'
}

export default function FieldLabel({ htmlFor, help, variant = 'block' }: FieldLabelProps) {
  const id = helpId(htmlFor)
  return (
    <>
      <div
        data-field-row
        className={
          variant === 'block' ? 'flex items-center gap-1.5 mb-1' : 'flex items-center gap-1.5'
        }
      >
        <label htmlFor={htmlFor} className="text-sm font-medium">
          {help.label}
        </label>
        {help.optional && <span className="text-xs text-muted-foreground">(optional)</span>}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label="More info"
              className="text-muted-foreground hover:text-foreground inline-flex"
            >
              <Info className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-left space-y-1">
            <p>{help.description}</p>
            {help.example && (
              <p>
                <span className="font-semibold">Example:</span> {help.example}
              </p>
            )}
          </TooltipContent>
        </Tooltip>
      </div>
      {/* The same content rendered for assistive tech — links to the input via
          aria-describedby. Hidden visually because the tooltip already shows it. */}
      <span id={id} className="sr-only">
        {help.description}
        {help.example ? ` Example: ${help.example}` : ''}
      </span>
    </>
  )
}
