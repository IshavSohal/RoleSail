import { render, screen } from '@testing-library/react'
import { Description } from './Description'

describe('Description', () => {
  it('renders recognized headings and treats source markup as text', () => {
    const { container } = render(<Description text={'Responsibilities\nBuild useful things\n<script>alert(1)</script>'} />)
    expect(screen.getByText('Responsibilities').tagName).toBe('STRONG')
    expect(screen.getByText('<script>alert(1)</script>')).toBeInTheDocument()
    expect(container.querySelector('script')).not.toBeInTheDocument()
  })

  it('shows an empty state', () => {
    render(<Description text="" empty="Nothing here" />)
    expect(screen.getByText('Nothing here')).toBeInTheDocument()
  })
})
