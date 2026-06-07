%-----------------------------------------------------------------------------%
% SPREAD-FAIL (extended): optimum-free OP check for pair {a,b} w.r.t. a model
% that ALREADY contains a set of committed disjunctions.  Same all-schedules
% dominance check as separable.mzn, but:
%   * the counterexample schedule must satisfy every committed disjunction;
%   * the scope-{a,b} repair must keep a (resp. b) disjoint from any committed
%     partner (which stays frozen).
% No optimum is used -- only the trivial horizon `ub` (= sum of durations).
%   UNSAT => {a,b} is optimality-preserving w.r.t. model + committed.
%-----------------------------------------------------------------------------%
include "globals.mzn";

int: n_res; set of int: Res = 1..n_res; array[Res] of int: rc;
int: n_tasks; set of int: Tasks = 1..n_tasks;
array[Tasks] of int: d; array[Res,Tasks] of int: rr; array[Tasks] of set of int: suc;

% committed disjunctions (1-indexed task pairs); supplied via a data file
int: n_committed;
array[1..n_committed, 1..2] of int: committed;

int: a; int: b;                                  % tested pair (pass via -D)
int: ub;                                         % trivial horizon (pass via -D)
set of int: TimesP = 0..(ub-1);

set of int: PREDa = { i | i in Tasks where a in suc[i] };
set of int: PREDb = { i | i in Tasks where b in suc[i] };

% committed partners of a and of b (the other endpoint of committed pairs)
set of int: CmtA = { committed[k,2] | k in 1..n_committed where committed[k,1] == a }
              union { committed[k,1] | k in 1..n_committed where committed[k,2] == a };
set of int: CmtB = { committed[k,2] | k in 1..n_committed where committed[k,1] == b }
              union { committed[k,1] | k in 1..n_committed where committed[k,2] == b };

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

% committed disjunctions hold in the counterexample
constraint forall(k in 1..n_committed)(
  let { int: c = committed[k,1], int: e = committed[k,2] } in
  s[c] + d[c] <= s[e] \/ s[e] + d[e] <= s[c] );

% a and b overlap (else trivially repaired)
constraint s[a] < s[b] + d[b] /\ s[b] < s[a] + d[a];

%--- free capacity profile of the FROZEN rest (all tasks except a, b) ------------
array[Res,TimesP] of var int: free =
  array2d(Res, TimesP,
    [ rc[r] - sum(j in Tasks where j!=a /\ j!=b)
                 ( rr[r,j] * bool2int(s[j] <= t /\ t < s[j]+d[j]) )
    | r in Res, t in TimesP ]);

%--- earliest / latest feasible start of a and b against the frozen rest ---------
var int: rela = max([0] ++ [ s[i]+d[i] | i in PREDa ]);
var int: relb = max([0] ++ [ s[i]+d[i] | i in PREDb ]);
var int: sucA = if suc[a]=={} then objective else min([ s[j] | j in suc[a] ]) endif;
var int: sucB = if suc[b]=={} then objective else min([ s[j] | j in suc[b] ]) endif;
var int: uba  = min(objective, sucA) - d[a];
var int: ubb  = min(objective, sucB) - d[b];

% placing a (resp. b) at t must respect resources AND committed partners (frozen)
predicate fitsA(int: t) =
  t >= rela /\ t <= uba /\
  forall(r in Res)( forall(tau in t..t+d[a]-1)( free[r,tau] >= rr[r,a] ) ) /\
  forall(x in CmtA)( t + d[a] <= s[x] \/ s[x] + d[x] <= t );
predicate fitsB(int: t) =
  t >= relb /\ t <= ubb /\
  forall(r in Res)( forall(tau in t..t+d[b]-1)( free[r,tau] >= rr[r,b] ) ) /\
  forall(x in CmtB)( t + d[b] <= s[x] \/ s[x] + d[x] <= t );

var int: ESa = min([ if fitsA(t) then t   else ub endif | t in 0..ub-d[a] ]);
var int: LSa = max([ if fitsA(t) then t   else -1 endif | t in 0..ub-d[a] ]);
var int: ESb = min([ if fitsB(t) then t   else ub endif | t in 0..ub-d[b] ]);
var int: LSb = max([ if fitsB(t) then t   else -1 endif | t in 0..ub-d[b] ]);

%--- assert NON-repairability ---------------------------------------------------
constraint ESa + d[a] > LSb;
constraint ESb + d[b] > LSa;

solve satisfy;
output [ "UNREPAIRABLE {", show(a), ",", show(b), "} makespan=", show(objective), "\n" ];
