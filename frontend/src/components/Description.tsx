const headings = /^(?:summary|job summary|description|role overview|position overview|overview|about (?:this|the) (?:role|job|position|team|company)|about us|about the team|key job responsibilities|job responsibilities|key responsibilities|responsibilities|duties|duties and responsibilities|what you(?:'|’)ll do|what you will do|minimum qualifications|basic qualifications|required qualifications|preferred qualifications|desired qualifications|qualifications|minimum requirements|required skills|preferred skills|skills and experience|education|experience|who you are|what we(?:'|’)re looking for|what we are looking for|what we offer|benefits|compensation|salary|salary range|pay range|pay transparency|work\/life balance|work-life balance|diverse experiences|inclusive team culture|mentorship and career growth|mentorship & career growth|why aws|why join us)\s*[:?]?\s*$/i

export function Description({ text, empty = 'No description is available yet.' }: { text?: string; empty?: string }) {
  if (!text) return <p className="muted">{empty}</p>
  return (
    <div className="formatted-text">
      {text.split(/\r?\n/).map((line, index) => {
        const display = line.replace(/^\s*#{1,6}\s+/, '').trim()
        const heading = display !== line.trim() || headings.test(display)
        if (!display) return <br key={index} />
        return heading
          ? <strong className="description-heading" key={index}>{display}</strong>
          : <span key={index}>{line}</span>
      })}
    </div>
  )
}

