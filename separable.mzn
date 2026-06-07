%-----------------------------------------------------------------------------%
% SPREAD-FAIL : dominance-checking CSP for an OP-disjoint pair D = {a, b}.
%
% Accepts the canonical rcpsp.mzn data (n_res, rc, n_tasks, d, rr, suc) plus the
% tested pair (a, b) and an upper bound `ub` on T* (default = trivial sum).
%
% It SEARCHES for a counterexample: a feasible schedule s with makespan <= ub in
% which a and b OVERLAP and cannot be separated by re-timing only {a,b} (the rest
% frozen) within s's own makespan.  Repair uses the two-task min/max-fit
% certificate of `pairwise-spreadability-conflict-graph.md`:
%     repairable  <=>  ES_a + d_a <= LS_b  OR  ES_b + d_b <= LS_a
% where ES/LS are the earliest/latest resource-feasible starts of a, b against
% the frozen rest within their precedence windows and the horizon = makespan(s).
%
%   UNSAT  =>  {a,b} is a separable (optimality-preserving) disjunction (scope {a,b})
%   SAT    =>  the printed schedule is an unrepairable witness (scope {a,b})
%-----------------------------------------------------------------------------%
include "globals.mzn";

int: n_res; set of int: Res = 1..n_res; array[Res] of int: rc;
int: n_tasks; set of int: Tasks = 1..n_tasks;
array[Tasks] of int: d; array[Res,Tasks] of int: rr; array[Tasks] of set of int: suc;

int: a; int: b;                                  % the tested pair (pass via -D)
int: ub;                                         % upper bound on T* (pass via -D)
set of int: TimesP = 0..(ub-1);                  % time points for the profile

set of int: PREDa = { i | i in Tasks where a in suc[i] };
set of int: PREDb = { i | i in Tasks where b in suc[i] };

%--- the counterexample schedule -------------------------------------------------
array[Tasks] of var 0..ub: s;
var 0..ub: objective;
constraint forall(i in Tasks)(s[i] + d[i] <= objective);
constraint objective <= ub;

% RCPSP feasibility
constraint forall(i in Tasks, j in suc[i])(s[i]+d[i] <= s[j]);
constraint forall(r in Res)(
  let { set of int: RT = { i | i in Tasks where rr[r,i]>0 /\ d[i]>0 } } in
  if RT != {} /\ sum(i in RT)(rr[r,i]) > rc[r] then
     cumulative([s[i]|i in RT],[d[i]|i in RT],[rr[r,i]|i in RT],rc[r]) else true endif);

% a and b overlap (else the pair is trivially repaired)
constraint s[a] < s[b] + d[b] /\ s[b] < s[a] + d[a];

%--- free capacity profile of the FROZEN rest (all tasks except a, b) ------------
array[Res,TimesP] of var int: free =
  array2d(Res, TimesP,
    [ rc[r] - sum(j in Tasks where j!=a /\ j!=b)
                 ( rr[r,j] * bool2int(s[j] <= t /\ t < s[j]+d[j]) )
    | r in Res, t in TimesP ]);

%--- earliest / latest resource-feasible start of a and b (frozen rest) ----------
% precedence window of a:  [rel_a, ub_a]
var int: rela = max([0] ++ [ s[i]+d[i] | i in PREDa ]);
var int: relb = max([0] ++ [ s[i]+d[i] | i in PREDb ]);
var int: sucA = if suc[a]=={} then objective else min([ s[j] | j in suc[a] ]) endif;
var int: sucB = if suc[b]=={} then objective else min([ s[j] | j in suc[b] ]) endif;
var int: uba  = min(objective, sucA) - d[a];
var int: ubb  = min(objective, sucB) - d[b];

% feasibility of placing a at start t against the frozen profile
predicate fitsA(int: t) =
  t >= rela /\ t <= uba /\
  forall(r in Res)( forall(tau in t..t+d[a]-1)( free[r,tau] >= rr[r,a] ) );
predicate fitsB(int: t) =
  t >= relb /\ t <= ubb /\
  forall(r in Res)( forall(tau in t..t+d[b]-1)( free[r,tau] >= rr[r,b] ) );

var int: ESa = min([ if fitsA(t) then t   else ub endif | t in 0..ub-d[a] ]);
var int: LSa = max([ if fitsA(t) then t   else -1 endif | t in 0..ub-d[a] ]);
var int: ESb = min([ if fitsB(t) then t   else ub endif | t in 0..ub-d[b] ]);
var int: LSb = max([ if fitsB(t) then t   else -1 endif | t in 0..ub-d[b] ]);

%--- assert NON-repairability ---------------------------------------------------
constraint ESa + d[a] > LSb;     % cannot place a before b
constraint ESb + d[b] > LSa;     % cannot place b before a

solve satisfy;

output [
  "UNREPAIRABLE counterexample for pair {", show(a), ",", show(b), "}\n",
  "makespan = ", show(objective), "\n",
  "s = ", show(s), "\n",
  "ESa,LSa = ", show(ESa), ",", show(LSa),
  "   ESb,LSb = ", show(ESb), ",", show(LSb), "\n"
];
