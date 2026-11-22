;; 内置 Wasm 校验器：逐套核对环境的连接上限与请求超时
;;
;; 导出：
;;   validate(conn_limit:i64, timeout_ms:i64) -> i64
;;     0 = 通过；1 = 连接上限超限；2 = 请求超时超限
;;     3 = 连接上限过小；4 = 请求超时过小
;;   checksum() -> i64            读取最近一次校验写入的状态指纹
;;   get_state() -> i64           读取可变全局（证明每项独立初始状态）
;;   bump_state() -> i64          自增可变全局并返回新值
;;   check_count (global)         可变全局的导出名，实例池据此复位
;;   memory: 1 页，保存最近一次校验的输入与结论
;;
;; 每次校验包含一段固定工作量的循环，使不同结论的指令消耗可观测。
(module
  ;; 允许的取值窗口（代理换版前的连接/超时基线）
  (global $conn_min i64 (i64.const 1))
  (global $conn_max i64 (i64.const 100000))
  (global $timeout_min i64 (i64.const 1))
  (global $timeout_max i64 (i64.const 60000))
  ;; 固定工作量：每次校验执行的处理循环轮数
  (global $work_rounds i64 (i64.const 2000))

  (memory (export "memory") 1)

  ;; 可变状态：每次实例化都从 0 开始，随校验次数累加。
  ;; 导出该可变全局，实例池在每次复用前按此处的初始值复位。
  (global $check_count (export "check_count") (mut i64) (i64.const 0))

  (func (export "checksum") (result i64)
    (i64.load (i32.const 8)))

  (func (export "get_state") (result i64)
    (global.get $check_count))

  (func (export "bump_state") (result i64)
    (global.set $check_count
      (i64.add (global.get $check_count) (i64.const 1)))
    (global.get $check_count))

  (func (export "validate") (param $conn_limit i64) (param $timeout_ms i64) (result i64)
    (local $verdict i64)
    (local $i i64)

    ;; 逐套核对：先连接上限，再请求超时（线性指令 + 命名块分支）
    (block $ok
      (block $fail_timeout_min
        (block $fail_conn_min
          (block $fail_timeout_max
            (block $fail_conn_max
              ;; 连接上限 > 最大值 → 结论 1
              (br_if $fail_conn_max
                (i64.gt_u (local.get $conn_limit) (global.get $conn_max)))
              ;; 请求超时 > 最大值 → 结论 2
              (br_if $fail_timeout_max
                (i64.gt_u (local.get $timeout_ms) (global.get $timeout_max)))
              ;; 连接上限 < 最小值 → 结论 3
              (br_if $fail_conn_min
                (i64.lt_u (local.get $conn_limit) (global.get $conn_min)))
              ;; 请求超时 < 最小值 → 结论 4
              (br_if $fail_timeout_min
                (i64.lt_u (local.get $timeout_ms) (global.get $timeout_min)))
              ;; 全部在窗口内 → 结论 0
              (local.set $verdict (i64.const 0))
              (br $ok)
            )
            (local.set $verdict (i64.const 1))
            (br $ok)
          )
          (local.set $verdict (i64.const 2))
          (br $ok)
        )
        (local.set $verdict (i64.const 3))
        (br $ok)
      )
      (local.set $verdict (i64.const 4))
    )

    ;; 固定处理工作量，模拟逐项核对过程，指令消耗因此稳定可观测
    (block $done
      (loop $cont
        (br_if $done (i64.ge_u (local.get $i) (global.get $work_rounds)))
        (local.set $i (i64.add (local.get $i) (i64.const 1)))
        (br $cont)))

    ;; 记录本次校验上下文到线性内存
    (i64.store (i32.const 0) (local.get $conn_limit))
    (i64.store (i32.const 8)
      (i64.add
        (i64.mul (local.get $timeout_ms) (i64.const 100001))
        (local.get $conn_limit)))
    (i64.store (i32.const 16) (local.get $verdict))

    (global.set $check_count
      (i64.add (global.get $check_count) (i64.const 1)))

    (local.get $verdict))
)
