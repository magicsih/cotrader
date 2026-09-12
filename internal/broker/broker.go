// Package broker holds the vocabulary both exchange adapters share.
package broker

import "errors"

// Error is a failure reported by an exchange adapter.
//
// Ambiguous marks the dangerous case: a state-changing request whose outcome we
// could not read. The order may or may not exist at the exchange, so the caller
// must look it up by identifier and must never send it again.
type Error struct {
	Code      string
	Ambiguous bool
}

func (e *Error) Error() string { return e.Code }

// Fail builds a definite failure.
func Fail(code string) *Error { return &Error{Code: code} }

// Unresolved builds a failure whose effect at the exchange is unknown.
func Unresolved(code string) *Error { return &Error{Code: code, Ambiguous: true} }

// Ambiguous reports whether err leaves the exchange in an unknown state. A true
// result means: reconcile by identifier, never resubmit.
func Ambiguous(err error) bool {
	var e *Error
	return errors.As(err, &e) && e.Ambiguous
}
