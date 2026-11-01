import { useState } from 'react'
import type { FormEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import type { WorkspaceQuestion } from '../api/types'

type JsonSchema = {
  type?: string
  title?: string
  description?: string
  enum?: unknown[]
  properties?: Record<string, JsonSchema>
  required?: string[]
  items?: JsonSchema
  minimum?: number
  maximum?: number
  minLength?: number
  maxLength?: number
}

function schemaOf(value: Record<string, unknown>): JsonSchema {
  return value as JsonSchema
}

function parseValue(value: string, schema: JsonSchema): unknown {
  if (schema.enum) {
    const option = schema.enum.find((item) => JSON.stringify(item) === value)
    if (option === undefined) throw new Error('Choose one of the listed answers.')
    return option
  }
  if (schema.type === 'integer' || schema.type === 'number') {
    const number = Number(value)
    if (!Number.isFinite(number)) throw new Error('Enter a valid number.')
    if (schema.type === 'integer' && !Number.isInteger(number)) {
      throw new Error('Enter a whole number.')
    }
    return number
  }
  if (schema.type === 'boolean') {
    if (value === 'true') return true
    if (value === 'false') return false
    throw new Error('Choose yes or no.')
  }
  if (schema.type === 'object' || schema.type === 'array') {
    try {
      const parsed: unknown = JSON.parse(value)
      if (schema.type === 'array' && !Array.isArray(parsed)) {
        throw new Error('Enter a JSON array.')
      }
      if (schema.type === 'object' && (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed))) {
        throw new Error('Enter a JSON object.')
      }
      return parsed
    } catch (error) {
      if (error instanceof Error && error.message.startsWith('Enter a JSON')) throw error
      throw new Error(`Enter valid JSON: ${error instanceof Error ? error.message : 'invalid value'}`)
    }
  }
  if (schema.minLength !== undefined && value.length < schema.minLength) {
    throw new Error(`Enter at least ${schema.minLength} characters.`)
  }
  if (schema.maxLength !== undefined && value.length > schema.maxLength) {
    throw new Error(`Use ${schema.maxLength} characters or fewer.`)
  }
  return value
}

function SchemaInput({
  schema,
  value,
  required,
  onChange,
}: {
  schema: JsonSchema
  value: string
  required: boolean
  onChange: (value: string) => void
}) {
  if (schema.enum) {
    return (
      <select required={required} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Choose an answer</option>
        {schema.enum.map((option) => {
          const serialized = JSON.stringify(option)
          return <option key={serialized} value={serialized}>{String(option)}</option>
        })}
      </select>
    )
  }
  if (schema.type === 'boolean') {
    return (
      <select required={required} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Choose an answer</option>
        <option value="true">Yes</option>
        <option value="false">No</option>
      </select>
    )
  }
  if (schema.type === 'integer' || schema.type === 'number') {
    return (
      <input
        type="number"
        step={schema.type === 'integer' ? 1 : 'any'}
        min={schema.minimum}
        max={schema.maximum}
        required={required}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  if (schema.type === 'string' || !schema.type) {
    return (
      <textarea
        required={required}
        minLength={schema.minLength}
        maxLength={schema.maxLength}
        rows={3}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }
  return (
    <textarea
      required={required}
      rows={4}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={schema.type === 'array' ? '["item"]' : '{"key":"value"}'}
    />
  )
}

export function QuestionCard({
  question,
  onAnswer,
}: {
  question: WorkspaceQuestion
  onAnswer: (answer: unknown) => Promise<void>
}) {
  const [singleValue, setSingleValue] = useState('')
  const [objectValues, setObjectValues] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const schema = schemaOf(question.response_schema)
  const properties = schema.type === 'object' ? schema.properties : undefined
  const requiredFields = new Set(schema.required ?? [])
  const editable = question.state === 'open'

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setError(null)
    try {
      let answer: unknown
      if (properties) {
        const result: Record<string, unknown> = {}
        for (const [name, fieldSchema] of Object.entries(properties)) {
          const raw = objectValues[name] ?? ''
          if (raw === '' && !requiredFields.has(name)) continue
          result[name] = parseValue(raw, fieldSchema)
        }
        answer = result
      } else {
        answer = parseValue(singleValue, schema)
      }
      setSubmitting(true)
      await onAnswer(answer)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The answer could not be submitted.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className={`question-card${editable ? '' : ' question-card--answered'}`} aria-label="Interview question">
      <div className="question-card__eyebrow"><span className="question-card__spark" aria-hidden="true">✳</span>{editable ? 'YOUR INPUT' : 'ANSWER SUBMITTED'}</div>
      <div className="question-card__prompt"><ReactMarkdown>{question.prompt}</ReactMarkdown></div>
      {!editable ? (
        <div className="question-card__accepted" aria-live="polite">
          <strong>Answer received</strong>
          <pre>{typeof question.answer === 'string' ? question.answer : JSON.stringify(question.answer, null, 2)}</pre>
          <span>The interview will continue with this answer.</span>
        </div>
      ) : (
        <form className="question-card__form" onSubmit={(event) => void submit(event)}>
          {properties ? Object.entries(properties).map(([name, fieldSchema]) => (
            <label className="question-card__field" key={name}>
              <span>{fieldSchema.title ?? name}{requiredFields.has(name) && <b aria-hidden="true"> *</b>}</span>
              {fieldSchema.description && <small>{fieldSchema.description}</small>}
              <SchemaInput
                schema={fieldSchema}
                value={objectValues[name] ?? ''}
                required={requiredFields.has(name)}
                onChange={(value) => setObjectValues((previous) => ({ ...previous, [name]: value }))}
              />
            </label>
          )) : (
            <label className="question-card__field">
              <span>{schema.title ?? 'Your answer'}</span>
              {schema.description && <small>{schema.description}</small>}
              <SchemaInput schema={schema} value={singleValue} required onChange={setSingleValue} />
            </label>
          )}
          {error && <p className="question-card__error" role="alert">{error}</p>}
          <button className="question-card__submit" type="submit" disabled={submitting}>
            {submitting ? 'Sending…' : 'Send answer'} <span aria-hidden="true">↑</span>
          </button>
        </form>
      )}
    </section>
  )
}
